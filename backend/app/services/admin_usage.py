"""平台管理员的精确用量对照与项目额度；不读取交付内容或任何凭据。"""

import csv
import io
from collections import defaultdict
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.events import record_event
from app.models import Project
from app.models.llm_usage import LLMProjectPolicy, ModelCall
from app.schemas.admin_usage import (
    AdminProjectInfo,
    ProjectQuotaUpdate,
    ProjectUsage,
    ReviewerUsage,
    UsageLimits,
    UsageReport,
    UsageTotals,
)

MAX_REPORT_DAYS = 366
MAX_REPORT_CALLS = 100_000
TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
)
CSV_FIELDS = (
    "date_utc",
    "project_id",
    "project_name",
    "reviewer",
    "model",
    "status",
    *TOKEN_FIELDS,
    "estimated_cost_cny",
    "call_id",
)


class AdminRequiredError(PermissionError):
    pass


class UsageRangeError(ValueError):
    pass


class AdminProjectNotFoundError(LookupError):
    pass


def require_admin(user_id: UUID, settings: Settings) -> None:
    if str(user_id) not in settings.admin_user_ids:
        raise AdminRequiredError("只有平台管理员可以查看用量或修改审核额度")


def _range(start: date, end: date) -> None:
    if type(start) is not date or type(end) is not date:
        raise UsageRangeError("请使用有效的UTC日期")
    if start > end or (end - start).days >= MAX_REPORT_DAYS:
        raise UsageRangeError("起止日期须按顺序填写，范围不超过366天（含首尾）")


def _rows(db: Session, start: date, end: date, project_id: UUID | None):
    _range(start, end)
    query = (
        select(
            ModelCall.id.label("call_id"),
            ModelCall.day,
            ModelCall.project_id,
            Project.name.label("project_name"),
            ModelCall.reviewer,
            ModelCall.model,
            ModelCall.status,
            *(getattr(ModelCall, field) for field in TOKEN_FIELDS),
            ModelCall.estimated_cost_cny,
        )
        .join(Project, Project.id == ModelCall.project_id)
        .where(ModelCall.day >= start, ModelCall.day <= end)
    )
    if project_id is not None:
        if db.scalar(select(Project.id).where(Project.id == project_id)) is None:
            raise AdminProjectNotFoundError
        query = query.where(ModelCall.project_id == project_id)
    rows = list(db.execute(query.order_by(ModelCall.day, ModelCall.id).limit(MAX_REPORT_CALLS + 1)))
    if len(rows) > MAX_REPORT_CALLS:
        raise UsageRangeError("调用记录超过100000条，请缩小日期范围或按项目筛选")
    return rows


def _totals(rows) -> dict:
    value = UsageTotals().model_dump()
    cost = Decimal("0")
    for row in rows:
        value["calls"] += 1
        measured = row.total_tokens is not None
        value["measured_calls"] += int(measured)
        value["unknown_usage_calls"] += int(not measured)
        value["unknown_cache_calls"] += int(row.cache_hit_tokens is None)
        value["unpriced_calls"] += int(row.estimated_cost_cny is None)
        for field in TOKEN_FIELDS:
            amount = getattr(row, field)
            if amount is not None:
                value[field] += amount
        if row.estimated_cost_cny is not None:
            cost += row.estimated_cost_cny
    value["estimated_cost_cny"] = format(cost, ".10f")
    return value


def usage_report(
    db: Session,
    user_id: UUID,
    settings: Settings,
    start: date,
    end: date,
    project_id: UUID | None = None,
) -> UsageReport:
    require_admin(user_id, settings)
    rows = _rows(db, start, end, project_id)
    projects, reviewers = defaultdict(list), defaultdict(list)
    for row in rows:
        projects[(row.project_id, row.project_name)].append(row)
        reviewers[row.reviewer].append(row)
    return UsageReport(
        start=start,
        end=end,
        totals=UsageTotals(**_totals(rows)),
        by_project=[
            ProjectUsage(project_id=key[0], project_name=key[1], **_totals(group))
            for key, group in sorted(projects.items(), key=lambda item: str(item[0][0]))
        ],
        by_reviewer=[
            ReviewerUsage(reviewer=key, **_totals(group))
            for key, group in sorted(reviewers.items())
        ],
        limits=UsageLimits(
            platform_daily_reviews=settings.llm_platform_daily_reviews,
            platform_daily_tokens=settings.llm_platform_daily_tokens,
            project_daily_reviews=settings.llm_project_daily_reviews,
            max_calls_per_review=settings.llm_max_calls_per_review,
        ),
    )


def _csv_cell(value) -> str:
    text = "" if value is None else str(value)
    if text[:1] in ("\t", "\r", "\n") or text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def usage_csv(
    db: Session,
    user_id: UUID,
    settings: Settings,
    start: date,
    end: date,
    project_id: UUID | None = None,
) -> str:
    require_admin(user_id, settings)
    rows = _rows(db, start, end, project_id)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(CSV_FIELDS)
    for row in rows:
        values = {
            **row._mapping,
            "date_utc": row.day.isoformat(),
            "estimated_cost_cny": format(row.estimated_cost_cny, ".10f")
            if row.estimated_cost_cny is not None
            else None,
        }
        writer.writerow(_csv_cell(values[field]) for field in CSV_FIELDS)
    return "\ufeff" + output.getvalue()


def list_projects(db: Session, user_id: UUID, settings: Settings) -> list[AdminProjectInfo]:
    require_admin(user_id, settings)
    return [
        AdminProjectInfo(
            id=identity,
            name=name,
            daily_review_limit=limit if limit is not None else settings.llm_project_daily_reviews,
        )
        for identity, name, limit in db.execute(
            select(Project.id, Project.name, LLMProjectPolicy.daily_review_limit)
            .outerjoin(LLMProjectPolicy, LLMProjectPolicy.project_id == Project.id)
            .order_by(Project.id)
        )
    ]


def update_project_quota(
    db: Session,
    user_id: UUID,
    settings: Settings,
    project_id: UUID,
    body: ProjectQuotaUpdate | dict,
) -> AdminProjectInfo:
    try:
        require_admin(user_id, settings)
        body = ProjectQuotaUpdate.model_validate(body)
        project = db.scalar(select(Project).where(Project.id == project_id))
        if project is None:
            raise AdminProjectNotFoundError
        # 只取额度行排他锁，不再反向等待 reserve_call 的平台日桶/项目日桶。
        db.execute(
            insert(LLMProjectPolicy)
            .values(
                project_id=project_id,
                daily_review_limit=settings.llm_project_daily_reviews,
                api_key_reference=None,
            )
            .on_conflict_do_nothing()
        )
        policy = db.scalar(
            select(LLMProjectPolicy)
            .where(LLMProjectPolicy.project_id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        old = policy.daily_review_limit
        if old != body.daily_review_limit:
            policy.daily_review_limit = body.daily_review_limit
            record_event(
                db,
                project_id=project_id,
                actor_id=user_id,
                event_type="llm_project_quota_updated",
                object_type="project",
                object_id=project_id,
                details={
                    "old_daily_review_limit": old,
                    "daily_review_limit": body.daily_review_limit,
                },
            )
        db.commit()
        return AdminProjectInfo(
            id=project.id, name=project.name, daily_review_limit=policy.daily_review_limit
        )
    except Exception:
        db.rollback()
        raise
