"""独立短事务预留与结算；不得在持有项目/工作流/队列锁时调用。"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.events import record_event
from app.models.delivery import DeliveryVersion
from app.models.llm_usage import DailyBudget, LLMProjectPolicy, ModelCall, ReviewAdmission
from app.models.review_job import ReviewJob


class LLMUsageError(ValueError):
    pass


class LLMDuplicateCallError(LLMUsageError):
    pass


class LLMCallLimitError(LLMUsageError):
    """一次审核的有限调用已经耗尽；次日不会重置此防循环限制。"""


class QuotaExceededError(Exception):
    def __init__(self, scope: str, retry_at: datetime):
        super().__init__("审核额度不足，审核已暂停")
        self.scope = scope
        self.retry_at = retry_at


@dataclass(frozen=True)
class QuotaLimits:
    project_daily_reviews: int = 20
    platform_daily_reviews: int = 200
    platform_daily_tokens: int = 1_000_000
    max_calls_per_review: int = 64

    def __post_init__(self):
        for value in (
            self.project_daily_reviews,
            self.platform_daily_reviews,
            self.platform_daily_tokens,
        ):
            if type(value) is not int or value < 0:
                raise ValueError("审核额度必须是非负整数")
        if type(self.max_calls_per_review) is not int or self.max_calls_per_review < 1:
            raise ValueError("单次审核调用上限必须是正整数")


def _now(db) -> datetime:
    return db.scalar(select(func.clock_timestamp())).astimezone(UTC)


def _budget(db, scope, day):
    db.execute(
        insert(DailyBudget)
        .values(scope_key=scope, day=day, review_count=0, reserved_tokens=0, charged_tokens=0)
        .on_conflict_do_nothing()
    )
    return db.scalar(
        select(DailyBudget)
        .where(DailyBudget.scope_key == scope, DailyBudget.day == day)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _event(db, call, job_id, event_type):
    delivery = db.scalar(select(DeliveryVersion).join(ReviewJob).where(ReviewJob.id == job_id))
    record_event(
        db,
        project_id=call.project_id,
        actor_type="agent",
        actor_id="platform-review-worker",
        event_type=event_type,
        object_type="delivery_version",
        object_id=delivery.id,
        task_id=delivery.module_id,
        plan_version_id=delivery.plan_version_id,
        spec_version_id=delivery.spec_version_id,
        details={
            "execution_origin": "platform_worker",
            "initiated_by": str(delivery.submitted_by),
            "call_id": str(call.id),
            "job_id": str(job_id),
            "reviewer": call.reviewer,
            "model": call.model,
            "status": call.status,
            "error_code": call.error_code,
        },
    )


def reserve_call(
    sessions,
    *,
    project_id: UUID,
    job_id: UUID,
    call_id: UUID,
    reviewer: str,
    instruction_version: str,
    model: str,
    endpoint: str,
    input_hash: str,
    reserved_tokens: int,
    prices: dict,
    limits: QuotaLimits | None = None,
) -> UUID:
    limits = limits or QuotaLimits()
    if (
        type(reserved_tokens) is not int
        or not 0 < reserved_tokens <= 10_000_000
        or re.fullmatch(r"[a-f0-9]{64}", input_hash) is None
        or not all(
            isinstance(value, str) and 0 < len(value) <= maximum
            for value, maximum in (
                (reviewer, 80),
                (instruction_version, 80),
                (model, 128),
                (endpoint, 255),
            )
        )
    ):
        raise LLMUsageError("模型调用元数据无效")
    with sessions() as db:
        try:
            job = db.scalar(
                select(ReviewJob)
                .join(DeliveryVersion)
                .where(ReviewJob.id == job_id, DeliveryVersion.project_id == project_id)
            )
            if job is None:
                raise LLMUsageError("正式交付审核任务不存在")
            current = _now(db)
            day = current.date()
            retry_at = datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC)
            # 平台日桶→项目日桶→审核名额→调用；网络发生在此事务提交之后。
            global_budget = _budget(db, "platform", day)
            project_budget = _budget(db, str(project_id), day)
            db.execute(
                insert(LLMProjectPolicy)
                .values(
                    project_id=project_id,
                    daily_review_limit=limits.project_daily_reviews,
                    api_key_reference=None,
                )
                .on_conflict_do_nothing()
            )
            policy = db.scalar(
                select(LLMProjectPolicy)
                .where(LLMProjectPolicy.project_id == project_id)
                .with_for_update()
            )
            if db.get(ModelCall, call_id) is not None:
                raise LLMDuplicateCallError("该模型调用标识已使用，不能重复外发")
            admission = db.scalar(
                select(ReviewAdmission).where(ReviewAdmission.job_id == job_id).with_for_update()
            )
            if admission is None:
                if project_budget.review_count >= policy.daily_review_limit:
                    raise QuotaExceededError("project_reviews", retry_at)
                if global_budget.review_count >= limits.platform_daily_reviews:
                    raise QuotaExceededError("platform_reviews", retry_at)
            elif admission.call_count >= limits.max_calls_per_review:
                raise LLMCallLimitError("单次审核模型调用次数已达上限，审核未完成")
            if (
                global_budget.reserved_tokens + global_budget.charged_tokens + reserved_tokens
                > limits.platform_daily_tokens
            ):
                raise QuotaExceededError("platform_tokens", retry_at)
            if admission is None:
                admission = ReviewAdmission(job_id=job_id, project_id=project_id, day=day)
                db.add(admission)
                db.flush()
                project_budget.review_count += 1
                global_budget.review_count += 1
            admission.call_count += 1
            global_budget.reserved_tokens += reserved_tokens
            project_budget.reserved_tokens += reserved_tokens
            call = ModelCall(
                id=call_id,
                admission_id=admission.id,
                project_id=project_id,
                day=day,
                reviewer=reviewer,
                instruction_version=instruction_version,
                model=model,
                endpoint=endpoint,
                input_hash=input_hash,
                reserved_tokens=reserved_tokens,
                prices={str(key): str(value) for key, value in prices.items()},
            )
            db.add(call)
            db.flush()
            _event(db, call, job_id, "llm_call_reserved")
            db.commit()
            return call.id
        except Exception:
            db.rollback()
            raise


def _usage(value):
    if not isinstance(value, dict):
        return None
    names = ("prompt_tokens", "completion_tokens", "total_tokens")
    if any(type(value.get(key)) is not int or value[key] < 0 for key in names):
        return None
    prompt, completion, total = (value[key] for key in names)
    if total != prompt + completion:
        return None
    hit, miss = value.get("prompt_cache_hit_tokens"), value.get("prompt_cache_miss_tokens")
    details = value.get("prompt_tokens_details")
    if "prompt_tokens_details" in value and not isinstance(details, dict):
        return None
    detailed_hit = details.get("cached_tokens") if isinstance(details, dict) else None
    for key, item in (
        ("prompt_cache_hit_tokens", hit),
        ("prompt_cache_miss_tokens", miss),
    ):
        if key in value and (type(item) is not int or item < 0):
            return None
    if isinstance(details, dict) and "cached_tokens" in details:
        if type(detailed_hit) is not int or detailed_hit < 0:
            return None
    completion_details = value.get("completion_tokens_details")
    if "completion_tokens_details" in value:
        if not isinstance(completion_details, dict):
            return None
        if "reasoning_tokens" in completion_details:
            reasoning = completion_details["reasoning_tokens"]
            if type(reasoning) is not int or not 0 <= reasoning <= completion:
                return None
    if hit is None and detailed_hit is not None:
        hit = detailed_hit
    if hit is not None and miss is None:
        miss = prompt - hit if type(hit) is int else None
    if miss is not None and hit is None:
        hit = prompt - miss if type(miss) is int else None
    if hit is not None or miss is not None:
        if (
            type(hit) is not int
            or type(miss) is not int
            or min(hit, miss) < 0
            or hit + miss != prompt
            or (detailed_hit is not None and (type(detailed_hit) is not int or detailed_hit != hit))
        ):
            return None
    return prompt, completion, total, hit, miss


def _cost(prices, usage):
    prompt, completion, total, hit, miss = usage
    if hit is None or prices.get("currency") != "CNY":
        return None
    try:
        values = [Decimal(prices[key]) for key in ("input_cache_hit", "input_cache_miss", "output")]
        if any(not value.is_finite() or value < 0 for value in values):
            return None
        return (hit * values[0] + miss * values[1] + completion * values[2]) / 1_000_000
    except (KeyError, TypeError, InvalidOperation):
        return None


def settle_call(
    sessions,
    call_id: UUID,
    *,
    usage: dict | None,
    status: str,
    error_code: str | None = None,
    output_hash: str | None = None,
) -> bool:
    if status not in {"succeeded", "failed"}:
        raise LLMUsageError("调用结算状态无效")
    if error_code is not None and re.fullmatch(r"[a-z_]{1,40}", error_code) is None:
        raise LLMUsageError("调用错误代码无效")
    if output_hash is not None and re.fullmatch(r"[a-f0-9]{64}", output_hash) is None:
        raise LLMUsageError("调用结果哈希无效")
    with sessions() as db:
        try:
            original = db.get(ModelCall, call_id)
            if original is None:
                raise LLMUsageError("模型调用记录不存在")
            # 按预留时的日桶结算；过期worker的真实用量也要保留。
            global_budget = _budget(db, "platform", original.day)
            project_budget = _budget(db, str(original.project_id), original.day)
            call = db.scalar(
                select(ModelCall)
                .where(ModelCall.id == call_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if call.status != "reserved":
                db.commit()
                return False
            measured = _usage(usage)
            call.output_hash = output_hash
            call.settled_at = _now(db)
            if measured is None:
                call.status = "unknown"
                call.error_code = error_code or (
                    "usage_missing" if usage is None else "usage_invalid"
                )
            else:
                prompt, completion, total, hit, miss = measured
                call.status, call.error_code = status, error_code
                call.prompt_tokens, call.completion_tokens, call.total_tokens = (
                    prompt,
                    completion,
                    total,
                )
                call.cache_hit_tokens, call.cache_miss_tokens = hit, miss
                call.charged_tokens = total
                call.estimated_cost_cny = _cost(call.prices, measured)
                for bucket in (global_budget, project_budget):
                    bucket.reserved_tokens -= call.reserved_tokens
                    bucket.charged_tokens += total
            admission = db.get(ReviewAdmission, call.admission_id)
            _event(db, call, admission.job_id, "llm_call_settled")
            db.commit()
            return True
        except Exception:
            db.rollback()
            raise
