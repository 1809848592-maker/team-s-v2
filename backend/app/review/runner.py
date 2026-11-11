"""后台子进程入口；导入模块不会读取环境、密钥或初始化数据库。"""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class DeliveryReviewInput:
    job_id: UUID
    delivery_id: UUID
    project_id: UUID
    attempt: int
    workflow_revision: int


class PipelineNotReadyError(Exception):
    """审核步骤未接入，不能生成通过或终审结论。"""


def run_delivery_review(review: DeliveryReviewInput) -> dict:
    from app.config import get_settings
    from app.db import SessionLocal
    from app.review.citation_databases import CitationDatabaseClient
    from app.review.factory import build_llm_client
    from app.services.review_pipeline import execute_pipeline
    from app.storage import LocalStorage

    settings = get_settings()
    return execute_pipeline(
        SessionLocal,
        review,
        storage=LocalStorage(settings.storage_dir),
        client=build_llm_client(SessionLocal, settings),
        database_client=CitationDatabaseClient(),
    )
