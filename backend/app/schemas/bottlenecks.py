from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class Bottleneck(BaseModel):
    kind: Literal["submission_overdue", "request_overdue", "resubmission_overdue"]
    module_id: UUID | None = None
    delivery_id: UUID | None = None
    request_id: UUID | None = None
    title: str
    # 严格超过此时刻才是卡点；退回类为退回时间加配置的天数。
    since: datetime


class ProjectBottlenecks(BaseModel):
    project_id: UUID
    evaluated_at: datetime
    return_after_days: int
    items: list[Bottleneck]
