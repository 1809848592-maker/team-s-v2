"""正式交付的内部只读文献核查；绑定快照查询结束后才读取存储及调用模型。"""

import hashlib
from uuid import UUID

from sqlalchemy import select

from app.models.delivery import DeliveryVersion
from app.models.plan import PlanVersion
from app.models.review_job import ReviewJob
from app.models.spec import SpecVersion
from app.review.citations import review_citations
from app.review.extract import extract_text
from app.review.extract_types import ExtractionLimits
from app.review.llm import LLMClient
from app.storage import Storage


class CitationReviewError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(
            {
                "source_unavailable": "正式交付、审核任务或绑定版本不可用，文献核查未完成。",
                "source_mismatch": "交付原文件与记录不一致，文献核查未完成。",
                "limit_exceeded": "交付超过处理上限，文献核查未完成。",
            }.get(code, "文献核查未完成。")
        )


def review_delivery_citations(
    sessions,
    *,
    project_id: UUID,
    delivery_id: UUID,
    job_id: UUID,
    storage: Storage,
    client: LLMClient,
    database_client,
    history: list | tuple,
    limits: ExtractionLimits | None = None,
) -> dict:
    """不新增公开权限，不处理私稿/共享材料，不改变交付、任务或业务事件。"""
    limits = limits if limits is not None else ExtractionLimits()
    with sessions() as db:
        source = db.execute(
            select(
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
        raise CitationReviewError("source_unavailable")
    if source.size > limits.max_file_bytes:
        raise CitationReviewError("limit_exceeded")
    try:
        with storage.path(source.sha256).open("rb") as stream:
            data = stream.read(limits.max_file_bytes + 1)
    except (OSError, ValueError):
        raise CitationReviewError("source_unavailable") from None
    if len(data) > limits.max_file_bytes:
        raise CitationReviewError("limit_exceeded")
    if len(data) != source.size or hashlib.sha256(data).hexdigest() != source.sha256:
        raise CitationReviewError("source_mismatch")
    document = extract_text(data, source.filename, limits=limits)
    result = review_citations(
        client,
        project_id=project_id,
        job_id=job_id,
        specification={"items": source.items},
        document=document,
        handoff_note=source.handoff_note,
        plan=source.content,
        history=history,
        database_client=database_client,
    )
    return {
        **result,
        "project_id": str(project_id),
        "delivery_id": str(delivery_id),
        "delivery_version": source.delivery_version,
        "job_id": str(job_id),
        "plan_version_id": str(source.plan_version_id),
        "plan_version": source.plan_version,
        "spec_version_id": str(source.spec_version_id),
        "spec_version": source.spec_version,
    }
