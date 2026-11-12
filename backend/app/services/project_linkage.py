"""负责人手动发起项目完整衔接审核；冻结来源，网络执行不持事务。"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.events import record_event
from app.models.delivery import DeliveryVersion
from app.models.project import Project, ProjectMember
from app.models.project_linkage import (
    ProjectLinkageRequest,
    ProjectLinkageResult,
    ProjectLinkageReview,
)
from app.models.review_workflow import DeliveryWorkflow
from app.schemas.collaboration import RequestCreate
from app.schemas.plans import PlanContent
from app.services import collaboration, model_disclosure
from app.services.plans import get_latest_plan
from app.services.projects import ProjectNotFoundError, require_member
from app.services.specs import get_latest_spec

WORKER_ACTOR = "platform-project-linkage-worker"
MAX_REPORT_BYTES = 2 * 1024 * 1024


class ProjectLinkageNotFoundError(ValueError):
    pass


class ProjectLinkageConflictError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectLinkageInput:
    review_id: UUID
    project_id: UUID
    attempt: int
    claim_token: UUID


def _now(db, override=None):
    value = override if override is not None else db.scalar(select(func.clock_timestamp()))
    if value.tzinfo is None:
        raise ValueError("审核时间必须带时区")
    return value.astimezone(UTC)


def _lock_project(db, project_id):
    if db.scalar(select(Project.id).where(Project.id == project_id).with_for_update()) is None:
        raise ProjectNotFoundError


def _leader(db, project_id, user_id):
    require_member(db, project_id, user_id, leader=True)
    _lock_project(db, project_id)
    db.get(ProjectMember, (project_id, user_id), populate_existing=True)
    require_member(db, project_id, user_id, leader=True)


def _event(db, item, event_type, *, user_id=None, details=None):
    record_event(
        db,
        project_id=item.project_id,
        actor_type="human" if user_id is not None else "agent",
        actor_id=user_id if user_id is not None else WORKER_ACTOR,
        event_type=event_type,
        object_type="project_linkage_review",
        object_id=item.id,
        plan_version_id=item.plan_version_id,
        spec_version_id=item.spec_version_id,
        details={
            "execution_origin": "platform_project_linkage",
            "initiated_by": str(item.requested_by),
            "attempt": item.attempt,
            "status": item.status,
            "error_code": item.error_code,
            **(details or {}),
        },
    )


def _item(db, project_id, review_id, *, lock=False):
    if lock:
        _lock_project(db, project_id)
    query = select(ProjectLinkageReview).where(
        ProjectLinkageReview.id == review_id, ProjectLinkageReview.project_id == project_id
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    item = db.scalar(query)
    if item is None:
        raise ProjectLinkageNotFoundError("项目衔接审核不存在")
    return item


def _public(db, item, user_id):
    member = require_member(db, item.project_id, user_id)
    result = json.loads(json.dumps(item.result)) if item.result is not None else None
    if result is not None:
        mappings = {
            str(row.finding_id): str(row.request_id)
            for row in db.scalars(
                select(ProjectLinkageRequest).where(ProjectLinkageRequest.review_id == item.id)
            )
        }
        for finding in result["findings"]:
            finding["request_id"] = mappings.get(finding["id"])
    message = {
        "queued": "完整衔接审核已排队，尚无审核结论。",
        "running": "正在逐模块摘要并进行整体比对，尚无审核结论。",
        "partial_failed": "完整衔接审核未完成，可由项目负责人重试。",
        "completed": "审核意见报告已生成，AI 生成，供参考。",
    }[item.status]
    if item.status == "completed" and result and result["coverage"] != "completed":
        message = "报告已生成，部分模块或范围未覆盖，不能视为通过。"
    if item.error_code == "quota_exceeded":
        message = "审核额度不足，完整衔接审核已暂停，尚无完整结论。"
    return {
        "id": str(item.id),
        "project_id": str(item.project_id),
        "requested_by": str(item.requested_by),
        "plan_version_id": str(item.plan_version_id),
        "plan_version": item.frozen_input["plan_version"],
        "spec_version_id": str(item.spec_version_id),
        "spec_version": item.frozen_input["spec_version"],
        "status": item.status,
        "attempt": item.attempt,
        "max_attempts": item.max_attempts,
        "error_code": item.error_code,
        "message": message,
        "created_at": item.created_at,
        "completed_at": item.completed_at,
        "can_retry": member.role == "leader" and item.status == "partial_failed",
        "can_convert": member.role == "leader"
        and item.status == "completed"
        and result is not None
        and bool(result["findings"]),
        "result": result,
    }


def start_review(db: Session, project_id: UUID, user_id: UUID):
    try:
        _leader(db, project_id, user_id)
        model_disclosure.require_review_consent(db, project_id)
        if (
            db.scalar(
                select(ProjectLinkageReview.id).where(
                    ProjectLinkageReview.project_id == project_id,
                    ProjectLinkageReview.status.in_(("queued", "running")),
                )
            )
            is not None
        ):
            raise ProjectLinkageConflictError("该项目已有排队或执行中的完整衔接审核")
        plan, spec = get_latest_plan(db, project_id), get_latest_spec(db, project_id)
        if plan is None or spec is None:
            raise ProjectLinkageConflictError("请先发布整体方案和团队规范")
        content = PlanContent.model_validate(plan.content)
        if not content.modules:
            raise ProjectLinkageConflictError("当前方案没有分工模块，无法发起完整衔接审核")
        selected = {
            item.module_id: item
            for item in db.scalars(
                select(DeliveryVersion)
                .join(DeliveryWorkflow, DeliveryWorkflow.delivery_id == DeliveryVersion.id)
                .where(
                    DeliveryVersion.project_id == project_id,
                    DeliveryVersion.module_id.in_([entry.id for entry in content.modules]),
                    DeliveryWorkflow.state == "adopted",
                )
                .distinct(DeliveryVersion.module_id)
                .order_by(DeliveryVersion.module_id, DeliveryVersion.version.desc())
            )
        }
        modules = []
        for module in content.modules:
            delivery = selected.get(module.id)
            modules.append(
                {
                    "module": module.model_dump(mode="json"),
                    "delivery": {
                        "id": str(delivery.id),
                        "version": delivery.version,
                        "filename": delivery.filename,
                        "sha256": delivery.sha256,
                        "size": delivery.size,
                        "handoff_note": delivery.handoff_note,
                        "plan_version_id": str(delivery.plan_version_id),
                        "spec_version_id": str(delivery.spec_version_id),
                    }
                    if delivery
                    else None,
                }
            )
        item = ProjectLinkageReview(
            project_id=project_id,
            requested_by=user_id,
            plan_version_id=plan.id,
            spec_version_id=spec.id,
            frozen_input={
                "plan": content.model_dump(mode="json"),
                "plan_version": plan.version,
                "spec_version": spec.version,
                "modules": modules,
                "disclosure_version": model_disclosure.DISCLOSURE_VERSION,
            },
            status="queued",
            attempt=0,
            max_attempts=3,
            available_at=_now(db),
        )
        db.add(item)
        db.flush()
        _event(
            db,
            item,
            "project_linkage_requested",
            user_id=user_id,
            details={"module_count": len(modules), "selection": "latest_adopted"},
        )
        db.commit()
        return _public(db, item, user_id)
    except Exception:
        db.rollback()
        raise


def list_reviews(db, project_id, user_id):
    require_member(db, project_id, user_id)
    items = db.scalars(
        select(ProjectLinkageReview)
        .where(ProjectLinkageReview.project_id == project_id)
        .order_by(ProjectLinkageReview.created_at.desc(), ProjectLinkageReview.id.desc())
        .limit(100)
    )
    return [_public(db, item, user_id) for item in items]


def get_review(db, project_id, review_id, user_id):
    require_member(db, project_id, user_id)
    return _public(db, _item(db, project_id, review_id), user_id)


def retry_review(db, project_id, review_id, user_id):
    try:
        _leader(db, project_id, user_id)
        model_disclosure.require_review_consent(db, project_id)
        item = _item(db, project_id, review_id, lock=True)
        if item.status != "partial_failed":
            raise ProjectLinkageConflictError("只有未完成的完整衔接审核可以重试")
        if (
            db.scalar(
                select(ProjectLinkageReview.id).where(
                    ProjectLinkageReview.project_id == project_id,
                    ProjectLinkageReview.id != item.id,
                    ProjectLinkageReview.status.in_(("queued", "running")),
                )
            )
            is not None
        ):
            raise ProjectLinkageConflictError("该项目已有排队或执行中的完整衔接审核")
        item.status, item.error_code, item.completed_at = "queued", None, None
        item.available_at = _now(db)
        item.max_attempts = max(item.max_attempts, item.attempt + 3)
        _event(db, item, "project_linkage_retry_scheduled", user_id=user_id)
        db.commit()
        return _public(db, item, user_id)
    except Exception:
        db.rollback()
        raise


def convert_finding_to_request(db, project_id, review_id, finding_id, user_id, body):
    try:
        _leader(db, project_id, user_id)
        item = _item(db, project_id, review_id, lock=True)
        if item.status != "completed":
            raise ProjectLinkageConflictError("请先完成完整衔接审核，再把意见分派为协作需求")
        finding = next(
            (
                entry
                for entry in (item.result or {}).get("findings", [])
                if entry["id"] == str(finding_id)
            ),
            None,
        )
        if finding is None:
            raise ProjectLinkageNotFoundError("该报告没有此审核意见")
        existing = db.get(ProjectLinkageRequest, (review_id, finding_id))
        if existing is not None:
            return collaboration.get_request(db, project_id, existing.request_id, user_id)
        body = RequestCreate.model_validate(body)

        def excerpt(value):
            return value if len(value) <= 1000 else value[:1000] + "…（节选，完整内容见原报告）"

        source = (
            f"完整衔接审核 {review_id} / 意见 {finding_id}\n"
            f"问题：{excerpt(finding['problem'])}\n依据：{excerpt(finding['basis'])}\n"
            f"摘录：{excerpt(finding['excerpt'])}\n建议：{excerpt(finding['suggestion'])}"
        )
        content = source + ("\n\n负责人补充：" + body.content if body.content else "")
        if len(content) > 10000:
            raise ProjectLinkageConflictError("意见和补充超过协作需求正文上限，请缩短补充")
        request_body = body.model_copy(update={"content": content})
        result = collaboration.create_request(db, project_id, user_id, request_body, commit=False)
        db.add(
            ProjectLinkageRequest(review_id=review_id, finding_id=finding_id, request_id=result.id)
        )
        db.flush()
        _event(
            db,
            item,
            "project_linkage_request_created",
            user_id=user_id,
            details={"finding_id": str(finding_id), "request_id": str(result.id)},
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def claim_next(
    db,
    *,
    timeout_seconds=300,
    lease_grace_seconds=5,
    max_attempts=3,
    retry_delay_seconds=5,
    now=None,
):
    if (
        timeout_seconds <= 0
        or lease_grace_seconds < 0
        or max_attempts < 1
        or retry_delay_seconds < 0
    ):
        raise ValueError("审核队列参数无效")
    current = _now(db, now)
    due = or_(
        and_(ProjectLinkageReview.status == "queued", ProjectLinkageReview.available_at <= current),
        and_(ProjectLinkageReview.status == "running", ProjectLinkageReview.lease_until <= current),
    )
    candidates = list(
        db.execute(
            select(ProjectLinkageReview.id, ProjectLinkageReview.project_id)
            .where(due)
            .order_by(ProjectLinkageReview.available_at, ProjectLinkageReview.id)
            .limit(20)
        )
    )
    for identity, project_id in candidates:
        project = db.scalar(
            select(Project.id).where(Project.id == project_id).with_for_update(skip_locked=True)
        )
        if project is None:
            continue
        item = db.scalar(
            select(ProjectLinkageReview)
            .where(ProjectLinkageReview.id == identity)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        current = _now(db, now)
        if (
            item.status not in {"queued", "running"}
            or (item.status == "queued" and item.available_at > current)
            or (item.status == "running" and item.lease_until > current)
        ):
            continue
        if item.status == "running":
            _event(db, item, "project_linkage_lease_expired")
            item.status = "queued" if item.attempt < item.max_attempts else "partial_failed"
            item.error_code, item.claim_token, item.lease_until = "lease_expired", None, None
            item.available_at = current + timedelta(seconds=retry_delay_seconds)
            item.completed_at = current if item.status == "partial_failed" else None
            continue
        if item.attempt == 0:
            item.max_attempts = max_attempts
        if item.attempt >= item.max_attempts:
            item.status, item.error_code, item.completed_at = (
                "partial_failed",
                "attempt_limit",
                current,
            )
            _event(db, item, "project_linkage_failed")
            continue
        item.status, item.attempt = "running", item.attempt + 1
        item.claim_token = uuid4()
        item.lease_until = current + timedelta(seconds=timeout_seconds + lease_grace_seconds)
        item.completed_at, item.error_code = None, None
        _event(db, item, "project_linkage_claimed")
        return ProjectLinkageInput(item.id, item.project_id, item.attempt, item.claim_token)
    return None


def _matches(item, claim, current):
    return (
        item.status == "running"
        and item.project_id == claim.project_id
        and (
            item.claim_token == claim.claim_token
            and item.attempt == claim.attempt
            and item.lease_until > current
        )
    )


def store_result(sessions, claim, result):
    with sessions() as db:
        item = _item(db, claim.project_id, claim.review_id, lock=True)
        if not _matches(item, claim, _now(db)):
            raise ProjectLinkageConflictError("项目审核领取凭证已失效")
        result = _validated_result(item, result)
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > MAX_REPORT_BYTES:
            raise ProjectLinkageConflictError("项目审核报告超过保存上限")
        record = ProjectLinkageResult(
            review_id=item.id,
            claim_token=claim.claim_token,
            attempt=claim.attempt,
            result=json.loads(encoded),
        )
        db.add(record)
        db.commit()
        return {"result_id": str(record.id)}


def _validated_result(item, raw):
    from app.schemas.project_linkage import LinkageResult

    result = LinkageResult.model_validate(raw).model_dump(mode="json")
    expected = item.frozen_input["modules"]
    if len(result["modules"]) != len(expected):
        raise ProjectLinkageConflictError("审核报告的模块范围与冻结输入不一致")
    summarized = set()

    def valid_provenance(provenance, reviewer):
        return (
            provenance is not None
            and provenance["reviewer"] == reviewer
            and bool(re.fullmatch(r"[a-f0-9]{64}", provenance["input_hash"]))
            and bool(provenance["model"])
            and bool(provenance["instruction_version"])
        )

    for module, entry in zip(result["modules"], expected, strict=True):
        source, delivery = entry["module"], entry["delivery"]
        required = {
            "module_id": source["id"],
            "module_name": source["name"],
            "delivery_id": delivery["id"] if delivery else None,
            "delivery_version": delivery["version"] if delivery else None,
            "plan_version_id": delivery["plan_version_id"] if delivery else None,
            "spec_version_id": delivery["spec_version_id"] if delivery else None,
            "plan_outdated": delivery is not None
            and delivery["plan_version_id"] != str(item.plan_version_id),
            "spec_outdated": delivery is not None
            and delivery["spec_version_id"] != str(item.spec_version_id),
        }
        if any(module[key] != value for key, value in required.items()):
            raise ProjectLinkageConflictError("审核报告的交付来源与冻结输入不一致")
        if (delivery is None) != (module["state"] == "uncovered"):
            raise ProjectLinkageConflictError("未覆盖模块的已采用来源不一致")
        if module["state"] == "summarized":
            if (
                delivery is None
                or not module["summary"]
                or not valid_provenance(module["provenance"], "project_summary")
            ):
                raise ProjectLinkageConflictError("摘要缺少已采用来源或审核员信息")
            summarized.add(module["module_id"])
        elif (
            module["summary"] is not None
            or module["provenance"] is not None
            or not module["reason"]
        ):
            raise ProjectLinkageConflictError("未覆盖模块不能带有已完成摘要")
    comparison = result["comparison"]
    if comparison["state"] == "completed":
        if not summarized or not valid_provenance(comparison["provenance"], "project_linkage"):
            raise ProjectLinkageConflictError("整体比对缺少模块摘要或审核员信息")
    elif comparison["state"] == "uncovered" and summarized:
        raise ProjectLinkageConflictError("已有模块摘要但整体比对缺失，不能当作完成")
    elif result["findings"]:
        # 部分无效意见可能使比较未完成，但保留下来的有效意见仍需有来源。
        if comparison["provenance"] is None:
            raise ProjectLinkageConflictError("审核意见缺少整体比对来源")
    seen = set()
    for finding in result["findings"]:
        identity = str(UUID(finding["id"]))
        if (
            identity in seen
            or not finding["module_ids"]
            or not set(finding["module_ids"]) <= summarized
            or not valid_provenance(finding, "project_linkage")
            or finding["request_id"] is not None
        ):
            raise ProjectLinkageConflictError("审核意见范围、来源或标识无效")
        seen.add(identity)
    if result["coverage"] == "completed" and (
        result["coverage_notes"]
        or len(summarized) != len(expected)
        or comparison["state"] != "completed"
    ):
        raise ProjectLinkageConflictError("未覆盖范围不能标记为已完成")
    return result


def complete_job(db, claim, result, *, now=None, retry_delay_seconds=5):
    item = _item(db, claim.project_id, claim.review_id, lock=True)
    current = _now(db, now)
    if not _matches(item, claim, current):
        return False
    try:
        record = db.get(ProjectLinkageResult, UUID(result["result_id"]))
    except (KeyError, TypeError, ValueError):
        record = None
    if (
        record is None
        or record.review_id != item.id
        or record.claim_token != claim.claim_token
        or record.attempt != claim.attempt
    ):
        return fail_job(db, claim, "execution_error", now=now)
    try:
        item.result = _validated_result(item, record.result)
    except (ValueError, TypeError, KeyError):
        return fail_job(db, claim, "execution_error", now=now)
    failed = record.result["comparison"]["state"] == "incomplete" or any(
        module["state"] == "failed" for module in record.result["modules"]
    )
    item.status = "partial_failed" if failed else "completed"
    item.error_code = "review_incomplete" if failed else None
    item.claim_token = item.lease_until = None
    item.completed_at = current
    _event(db, item, "project_linkage_failed" if failed else "project_linkage_completed")
    return True


def fail_job(
    db, claim, error_code, *, retryable=True, retry_delay_seconds=5, retry_at=None, now=None
):
    item = _item(db, claim.project_id, claim.review_id, lock=True)
    current = _now(db, now)
    if not _matches(item, claim, current):
        return False
    if error_code not in {"execution_error", "timeout", "quota_exceeded", "pipeline_not_ready"}:
        error_code = "execution_error"
    if error_code == "quota_exceeded":
        item.attempt -= 1
        item.status = "queued"
        item.available_at = retry_at or datetime.combine(
            current.date() + timedelta(days=1), datetime.min.time(), UTC
        )
        if item.available_at <= current:
            item.available_at = current + timedelta(seconds=60)
    else:
        again = retryable and item.attempt < item.max_attempts
        item.status = "queued" if again else "partial_failed"
        item.available_at = current + timedelta(seconds=retry_delay_seconds)
    item.error_code, item.claim_token, item.lease_until = error_code, None, None
    item.completed_at = current if item.status == "partial_failed" else None
    _event(
        db,
        item,
        "project_linkage_retry_scheduled" if item.status == "queued" else "project_linkage_failed",
    )
    return True
