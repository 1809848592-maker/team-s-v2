"""固定审核编排：短事务取历史，事务外运行，完整报告持久化后仅传引用。"""

from uuid import UUID

from sqlalchemy import func, select

from app.models.delivery import DeliveryVersion
from app.models.review_job import ReviewJob
from app.models.review_workflow import DeliveryWorkflow
from app.review.runner import DeliveryReviewInput
from app.services.citation_review import review_delivery_citations
from app.services.format_check import inspect_delivery_format
from app.services.llm_usage import QuotaExceededError
from app.services.reasoning_review import review_delivery_reasoning
from app.services.spec_review import review_delivery_spec


class PipelineSourceError(ValueError):
    def __init__(self) -> None:
        super().__init__("审核任务或正式交付当前不可用，本次审核未完成。")


def _load_history(sessions, review: DeliveryReviewInput):
    from app.services.review_reports import load_review_history

    if (
        not isinstance(review, DeliveryReviewInput)
        or any(
            not isinstance(value, UUID)
            for value in (review.project_id, review.delivery_id, review.job_id)
        )
        or type(review.attempt) is not int
        or review.attempt < 1
        or type(review.workflow_revision) is not int
        or review.workflow_revision < 1
    ):
        raise PipelineSourceError
    with sessions() as db:
        item = db.scalar(
            select(DeliveryVersion)
            .join(ReviewJob, ReviewJob.delivery_id == DeliveryVersion.id)
            .join(DeliveryWorkflow, DeliveryWorkflow.delivery_id == DeliveryVersion.id)
            .where(
                DeliveryVersion.id == review.delivery_id,
                DeliveryVersion.project_id == review.project_id,
                ReviewJob.id == review.job_id,
                ReviewJob.status == "running",
                ReviewJob.attempt == review.attempt,
                ReviewJob.workflow_revision == review.workflow_revision,
                ReviewJob.lease_until > func.clock_timestamp(),
                DeliveryWorkflow.state == "ai_reviewing",
                DeliveryWorkflow.revision == review.workflow_revision,
            )
        )
        if item is None:
            raise PipelineSourceError
        return load_review_history(db, item)


def execute_pipeline(sessions, review, *, storage, client, database_client) -> dict:
    """固定四阶段。失败不抹掉其它阶段结果，额度暂停仍交由worker处理。"""
    from app.services.review_reports import stage_report

    history = _load_history(sessions, review)
    common = {
        "project_id": review.project_id,
        "delivery_id": review.delivery_id,
        "storage": storage,
    }
    modeled = {**common, "job_id": review.job_id, "client": client, "history": history}
    stages = (
        ("format", inspect_delivery_format, common),
        ("specification", review_delivery_spec, modeled),
        (
            "citations",
            review_delivery_citations,
            {**modeled, "database_client": database_client},
        ),
        ("reasoning", review_delivery_reasoning, modeled),
    )
    components = {}
    for name, operation, arguments in stages:
        try:
            result = operation(sessions, **arguments)
            if not isinstance(result, dict):
                raise TypeError
            components[name] = result
        except QuotaExceededError:
            raise
        except Exception:
            # 不复制异常、文件路径、正文、厂商响应或密钥到报告。
            components[name] = {
                "status": "incomplete",
                "error_code": "execution_error",
                "summary": "此审核步骤未完成，请稍后重试。",
            }
    return stage_report(sessions, review, components)
