"""正式交付的内部只读逻辑与衔接审核；绑定快照查询结束后才读取存储及调用模型。"""

import hashlib
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import aliased

from app.models.delivery import DeliveryVersion
from app.models.plan import PlanVersion
from app.models.review_job import ReviewJob
from app.models.review_workflow import DeliveryWorkflow
from app.models.spec import SpecVersion
from app.review.extract import extract_text
from app.review.extract_types import ExtractionLimits
from app.review.llm import LLMClient
from app.review.reasoning import review_reasoning
from app.review.reviewers.reasoning_types import UpstreamSnapshot
from app.schemas.plans import PlanContent
from app.storage import Storage


class ReasoningReviewError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(
            {
                "source_unavailable": "正式交付、审核任务或绑定版本不可用，逻辑与衔接审核未完成。",
                "source_mismatch": "交付原文件与记录不一致，逻辑与衔接审核未完成。",
                "limit_exceeded": "交付超过处理上限，逻辑与衔接审核未完成。",
            }.get(code, "逻辑与衔接审核未完成。")
        )


def review_delivery_reasoning(
    sessions,
    *,
    project_id: UUID,
    delivery_id: UUID,
    job_id: UUID,
    storage: Storage,
    client: LLMClient,
    history: list | tuple,
    limits: ExtractionLimits | None = None,
) -> dict:
    """不新增公开权限，不处理私稿/共享材料，不改变交付、任务或业务事件。"""
    limits = limits if limits is not None else ExtractionLimits()
    with sessions() as db:
        source = db.execute(
            select(
                DeliveryVersion.module_id,
                DeliveryVersion.filename,
                DeliveryVersion.sha256,
                DeliveryVersion.size,
                DeliveryVersion.version.label("delivery_version"),
                DeliveryVersion.handoff_note,
                SpecVersion.id.label("spec_version_id"),
                SpecVersion.version.label("spec_version"),
                SpecVersion.items,
                PlanVersion.id.label("plan_version_id"),
                PlanVersion.version.label("plan_version"),
                PlanVersion.content,
            )
            .select_from(DeliveryVersion)
            .join(
                SpecVersion,
                (SpecVersion.id == DeliveryVersion.spec_version_id)
                & (SpecVersion.project_id == DeliveryVersion.project_id),
            )
            .join(
                PlanVersion,
                (PlanVersion.id == DeliveryVersion.plan_version_id)
                & (PlanVersion.project_id == DeliveryVersion.project_id),
            )
            .join(
                ReviewJob,
                (ReviewJob.delivery_id == DeliveryVersion.id) & (ReviewJob.id == job_id),
            )
            .where(DeliveryVersion.id == delivery_id, DeliveryVersion.project_id == project_id)
        ).one_or_none()
        if source is None:
            raise ReasoningReviewError("source_unavailable")
        try:
            content = PlanContent.model_validate(source.content)
            modules = {module.id: module for module in content.modules}
            if len(modules) != len(content.modules) or source.module_id not in modules:
                raise ValueError
            task = modules[source.module_id]
            dependencies = list(dict.fromkeys(entry.module_id for entry in task.upstream))
            if (
                len(dependencies) != len(task.upstream)
                or source.module_id in dependencies
                or not set(dependencies) <= modules.keys()
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise ReasoningReviewError("source_unavailable") from None
        selected = {}
        if dependencies:
            upstream_plan, upstream_spec = aliased(PlanVersion), aliased(SpecVersion)
            candidates = db.execute(
                select(
                    DeliveryVersion.project_id,
                    DeliveryVersion.module_id,
                    DeliveryVersion.id.label("delivery_id"),
                    DeliveryVersion.version,
                    DeliveryVersion.plan_version_id,
                    DeliveryVersion.spec_version_id,
                    DeliveryVersion.sha256,
                    DeliveryVersion.handoff_note,
                    DeliveryWorkflow.state,
                    DeliveryWorkflow.revision.label("workflow_revision"),
                )
                .select_from(DeliveryVersion)
                .join(DeliveryWorkflow, DeliveryWorkflow.delivery_id == DeliveryVersion.id)
                .join(
                    upstream_plan,
                    (upstream_plan.id == DeliveryVersion.plan_version_id)
                    & (upstream_plan.project_id == DeliveryVersion.project_id),
                )
                .join(
                    upstream_spec,
                    (upstream_spec.id == DeliveryVersion.spec_version_id)
                    & (upstream_spec.project_id == DeliveryVersion.project_id),
                )
                .where(
                    DeliveryVersion.project_id == project_id,
                    DeliveryVersion.module_id.in_(dependencies),
                    DeliveryWorkflow.state == "adopted",
                )
                .distinct(DeliveryVersion.module_id)
                .order_by(DeliveryVersion.module_id, DeliveryVersion.version.desc())
            )
            for candidate in candidates:
                if candidate.module_id not in selected:
                    try:
                        selected[candidate.module_id] = UpstreamSnapshot.model_validate(
                            dict(candidate._mapping)
                        ).model_dump(mode="json")
                    except (ValueError, TypeError):
                        raise ReasoningReviewError("source_unavailable") from None
        upstream = [selected[module_id] for module_id in dependencies if module_id in selected]
    if source.size > limits.max_file_bytes:
        raise ReasoningReviewError("limit_exceeded")
    try:
        with storage.path(source.sha256).open("rb") as stream:
            data = stream.read(limits.max_file_bytes + 1)
    except (OSError, ValueError):
        raise ReasoningReviewError("source_unavailable") from None
    if len(data) > limits.max_file_bytes:
        raise ReasoningReviewError("limit_exceeded")
    if len(data) != source.size or hashlib.sha256(data).hexdigest() != source.sha256:
        raise ReasoningReviewError("source_mismatch")
    document = extract_text(data, source.filename, limits=limits)
    result = review_reasoning(
        client,
        project_id=project_id,
        job_id=job_id,
        module_id=source.module_id,
        specification={"items": source.items},
        document=document,
        handoff_note=source.handoff_note,
        plan=source.content,
        history=history,
        upstream=upstream,
    )
    return {
        **result,
        "project_id": str(project_id),
        "delivery_id": str(delivery_id),
        "module_id": str(source.module_id),
        "delivery_version": source.delivery_version,
        "job_id": str(job_id),
        "plan_version_id": str(source.plan_version_id),
        "plan_version": source.plan_version,
        "spec_version_id": str(source.spec_version_id),
        "spec_version": source.spec_version,
    }
