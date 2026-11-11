"""审核流水线入口；后续任务逐项接入实际检查。"""

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
    raise PipelineNotReadyError
