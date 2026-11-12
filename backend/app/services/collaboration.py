from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.events import record_event
from app.models.agent import Agent
from app.models.collaboration import CollaborationRequest, ProgressReport, RequestMessage
from app.models.delivery import DeliveryVersion
from app.models.module import Module
from app.models.project import Project, ProjectMember
from app.schemas.collaboration import (
    MessageCreate,
    ModuleProgress,
    ProgressCreate,
    ProgressInfo,
    RequestCreate,
    RequestInfo,
    RequestMessageInfo,
)
from app.schemas.plans import ModuleContent, PlanContent
from app.services import agent_permissions, notifications
from app.services.modules import get_current_module
from app.services.plans import get_latest_plan
from app.services.projects import ProjectNotFoundError, require_member


class CollaborationNotFoundError(Exception):
    pass


class CollaborationPermissionError(Exception):
    pass


class CollaborationStateError(Exception):
    pass


class CollaborationValidationError(ValueError):
    pass


def _validated[Model: BaseModel](schema: type[Model], body: Model | dict) -> Model:
    try:
        return schema.model_validate(body)
    except ValidationError:
        raise CollaborationValidationError("请检查阶段、摘要、需求对象和正文等输入字段") from None


def _lock_project(db: Session, project_id: UUID) -> None:
    if db.scalar(select(Project.id).where(Project.id == project_id).with_for_update()) is None:
        raise ProjectNotFoundError


def _authorize_agent(
    db: Session,
    agent: Agent,
    action: str,
    project_id: UUID,
    user_id: UUID,
    *,
    module_id: UUID | None = None,
    request_id: UUID | None = None,
    lock: bool = False,
) -> UUID:
    context = {"project_id": project_id, "module_id": module_id, "request_id": request_id}
    with db.no_autoflush:
        agent_permissions.authorize(db, agent, action, **context)
        identity = inspect(agent).identity
        agent_id = identity[0] if identity is not None else agent.id
        query = select(Agent).where(Agent.id == agent_id)
        if lock:
            # 写入结束前保持令牌有效性，吊销会等待本次已授权事务结束。
            query = query.with_for_update(read=True)
        principal = db.scalar(query.execution_options(populate_existing=True))
        if principal is None:
            agent_permissions.deny(db, agent, action, "agent_inactive", **context)
        if lock:
            agent_permissions.authorize(db, agent, action, **context)
        if principal.owner_id != user_id:
            agent_permissions.deny(db, agent, action, "owner_scope", **context)
        return principal.id


def _actor(user_id: UUID, agent: Agent | None) -> dict:
    if agent is None:
        return {"actor_id": user_id}
    return {
        "actor_id": _agent_id(agent),
        "actor_type": "agent",
        "represented_user_id": user_id,
    }


def _agent_id(agent: Agent | None) -> UUID | None:
    if agent is None:
        return None
    identity = inspect(agent).identity
    return identity[0] if identity is not None else agent.id


def _current_module(db: Session, project_id: UUID, module_id: UUID) -> ModuleContent:
    plan = get_latest_plan(db, project_id)
    module = (
        next(
            (
                item
                for item in PlanContent.model_validate(plan.content).modules
                if item.id == module_id
            ),
            None,
        )
        if plan is not None
        else None
    )
    if module is None:
        raise CollaborationNotFoundError
    return module


def report_progress(
    db: Session,
    project_id: UUID,
    module_id: UUID,
    user_id: UUID,
    body: ProgressCreate | dict,
    *,
    agent: Agent | None = None,
) -> ProgressReport:
    if agent is not None:
        _authorize_agent(db, agent, "report_progress", project_id, user_id, module_id=module_id)
    require_member(db, project_id, user_id)
    body = _validated(ProgressCreate, body)
    _lock_project(db, project_id)
    if agent is not None:
        _authorize_agent(
            db, agent, "report_progress", project_id, user_id, module_id=module_id, lock=True
        )
    module = _current_module(db, project_id, module_id)
    if user_id not in module.assignee_ids:
        raise CollaborationPermissionError("只有该模块的负责人可以回报进度")
    plan = get_latest_plan(db, project_id)
    item = ProgressReport(
        project_id=project_id,
        module_id=module_id,
        user_id=user_id,
        agent_id=_agent_id(agent),
        stage=body.stage,
        summary=body.summary,
        plan_version_id=plan.id,
        created_at=datetime.now(UTC),
    )
    db.add(item)
    db.flush()
    record_event(
        db,
        project_id=project_id,
        **_actor(user_id, agent),
        event_type="progress_reported",
        object_type="progress_report",
        object_id=item.id,
        task_id=module_id,
        plan_version_id=plan.id,
        details={"stage": item.stage, "summary": item.summary, "source": item.source},
    )
    db.commit()
    return item


def list_progress(
    db: Session, project_id: UUID, module_id: UUID, user_id: UUID
) -> list[ProgressReport]:
    require_member(db, project_id, user_id)
    module = db.scalar(
        select(Module.id).where(Module.id == module_id, Module.project_id == project_id)
    )
    if module is None:
        raise CollaborationNotFoundError
    return list(
        db.scalars(
            select(ProgressReport)
            .where(ProgressReport.project_id == project_id, ProgressReport.module_id == module_id)
            .order_by(ProgressReport.created_at.desc(), ProgressReport.id.desc())
        )
    )


def project_progress(db: Session, project_id: UUID, user_id: UUID) -> list[ModuleProgress]:
    require_member(db, project_id, user_id)
    plan = get_latest_plan(db, project_id)
    if plan is None:
        return []
    modules = PlanContent.model_validate(plan.content).modules
    module_ids = [item.id for item in modules]
    if not module_ids:
        return []
    reports = {
        item.module_id: item
        for item in db.scalars(
            select(ProgressReport)
            .where(
                ProgressReport.project_id == project_id, ProgressReport.module_id.in_(module_ids)
            )
            .distinct(ProgressReport.module_id)
            .order_by(
                ProgressReport.module_id, ProgressReport.created_at.desc(), ProgressReport.id.desc()
            )
        )
    }
    deliveries = {
        item.module_id: item
        for item in db.scalars(
            select(DeliveryVersion)
            .where(
                DeliveryVersion.project_id == project_id, DeliveryVersion.module_id.in_(module_ids)
            )
            .distinct(DeliveryVersion.module_id)
            .order_by(DeliveryVersion.module_id, DeliveryVersion.version.desc())
        )
    }
    result = []
    for module in modules:
        report = reports.get(module.id)
        delivery = deliveries.get(module.id)
        result.append(
            ModuleProgress(
                module_id=module.id,
                stage="submitted" if delivery else report.stage if report else "not_started",
                last_report=ProgressInfo.model_validate(report) if report else None,
                delivery_id=delivery.id if delivery else None,
            )
        )
    return result


def _target_users(
    db: Session,
    project_id: UUID,
    target_user_id: UUID | None,
    target_module_id: UUID | None,
    *,
    must_exist: bool = False,
) -> set[UUID]:
    recipients = set()
    if target_user_id is not None:
        if db.get(ProjectMember, (project_id, target_user_id)) is not None:
            recipients.add(target_user_id)
    elif target_module_id is not None:
        module = get_current_module(db, project_id, target_module_id)
        if module is not None:
            recipients.update(module.assignee_ids)
    if must_exist and not recipients:
        raise CollaborationNotFoundError
    return recipients


def _request_infos(db: Session, items: list[CollaborationRequest]) -> list[RequestInfo]:
    if not items:
        return []
    messages: dict[UUID, list[RequestMessageInfo]] = {item.id: [] for item in items}
    for message in db.scalars(
        select(RequestMessage)
        .where(RequestMessage.request_id.in_(messages))
        .order_by(RequestMessage.created_at, RequestMessage.id)
    ):
        messages[message.request_id].append(RequestMessageInfo.model_validate(message))
    return [
        RequestInfo.model_validate(item).model_copy(update={"messages": messages[item.id]})
        for item in items
    ]


def _get_request(
    db: Session, project_id: UUID, request_id: UUID, *, lock: bool = False
) -> CollaborationRequest:
    query = select(CollaborationRequest).where(
        CollaborationRequest.id == request_id, CollaborationRequest.project_id == project_id
    )
    if lock:
        # 所有变更统一先锁项目、再锁需求，与方案发布使用相同的权限检查时点。
        _lock_project(db, project_id)
        query = query.with_for_update().execution_options(populate_existing=True)
    item = db.scalar(query)
    if item is None:
        raise CollaborationNotFoundError
    return item


def _finish_request_change(
    db: Session,
    item: CollaborationRequest,
    user_id: UUID,
    event_type: str,
    message: str,
    *,
    recipients: set[UUID] | None = None,
    details: dict | None = None,
    agent: Agent | None = None,
    commit: bool = True,
) -> RequestInfo:
    if recipients is None:
        recipients = _target_users(db, item.project_id, item.target_user_id, item.target_module_id)
        recipients.add(item.created_by)
        if item.accepted_by is not None:
            recipients.add(item.accepted_by)
    record_event(
        db,
        project_id=item.project_id,
        **_actor(user_id, agent),
        event_type=event_type,
        object_type="collaboration_request",
        object_id=item.id,
        task_id=item.target_module_id,
        details={"status": item.status, **(details or {})},
    )
    notifications.notify_request_change(
        db,
        project_id=item.project_id,
        request_id=item.id,
        kind=event_type,
        recipient_ids=recipients,
        message=f"{item.title}：{message}",
    )
    result = _request_infos(db, [item])[0]
    if commit:
        db.commit()
    else:
        db.flush()
    return result


def create_request(
    db: Session,
    project_id: UUID,
    user_id: UUID,
    body: RequestCreate | dict,
    *,
    agent: Agent | None = None,
    commit: bool = True,
) -> RequestInfo:
    if agent is not None:
        _authorize_agent(db, agent, "create_request", project_id, user_id)
    require_member(db, project_id, user_id)
    body = _validated(RequestCreate, body)
    _lock_project(db, project_id)
    if agent is not None:
        _authorize_agent(db, agent, "create_request", project_id, user_id, lock=True)
    recipients = _target_users(
        db, project_id, body.target_user_id, body.target_module_id, must_exist=True
    )
    item = CollaborationRequest(
        project_id=project_id,
        created_by=user_id,
        title=body.title,
        content=body.content,
        target_user_id=body.target_user_id,
        target_module_id=body.target_module_id,
        expected_by=body.expected_by,
        status="open",
        created_at=datetime.now(UTC),
    )
    db.add(item)
    db.flush()
    return _finish_request_change(
        db,
        item,
        user_id,
        "request_created",
        "收到新的协作需求",
        recipients=recipients,
        agent=agent,
        commit=commit,
        details={
            "title": item.title,
            "target_user_id": str(item.target_user_id) if item.target_user_id else None,
            "target_module_id": str(item.target_module_id) if item.target_module_id else None,
            "expected_by": item.expected_by.isoformat() if item.expected_by else None,
        },
    )


def list_requests(db: Session, project_id: UUID, user_id: UUID) -> list[RequestInfo]:
    require_member(db, project_id, user_id)
    items = list(
        db.scalars(
            select(CollaborationRequest)
            .where(CollaborationRequest.project_id == project_id)
            .order_by(CollaborationRequest.created_at.desc(), CollaborationRequest.id.desc())
        )
    )
    return _request_infos(db, items)


def get_request(db: Session, project_id: UUID, request_id: UUID, user_id: UUID) -> RequestInfo:
    require_member(db, project_id, user_id)
    return _request_infos(db, [_get_request(db, project_id, request_id)])[0]


def accept_request(
    db: Session,
    project_id: UUID,
    request_id: UUID,
    user_id: UUID,
    *,
    agent: Agent | None = None,
) -> RequestInfo:
    if agent is not None:
        _authorize_agent(db, agent, "accept_request", project_id, user_id, request_id=request_id)
    require_member(db, project_id, user_id)
    item = _get_request(db, project_id, request_id, lock=True)
    if agent is not None:
        _authorize_agent(
            db, agent, "accept_request", project_id, user_id, request_id=request_id, lock=True
        )
    targets = _target_users(
        db, project_id, item.target_user_id, item.target_module_id, must_exist=True
    )
    if user_id not in targets:
        raise CollaborationPermissionError("只有需求指定的人或模块负责人可以接手")
    if item.status != "open":
        raise CollaborationStateError("只有尚未接手的需求可以接手")
    item.accepted_by = user_id
    item.accepted_at = datetime.now(UTC)
    item.status = "accepted"
    return _finish_request_change(db, item, user_id, "request_accepted", "需求已接手", agent=agent)


def _append_message(
    db: Session,
    item: CollaborationRequest,
    user_id: UUID,
    body: MessageCreate,
    kind: str,
    *,
    agent: Agent | None = None,
) -> RequestMessage:
    message = RequestMessage(
        request_id=item.id,
        author_id=user_id,
        agent_id=_agent_id(agent),
        kind=kind,
        content=body.content,
        created_at=datetime.now(UTC),
    )
    db.add(message)
    db.flush()
    return message


def respond_request(
    db: Session,
    project_id: UUID,
    request_id: UUID,
    user_id: UUID,
    body: MessageCreate | dict,
    *,
    agent: Agent | None = None,
) -> RequestInfo:
    if agent is not None:
        _authorize_agent(db, agent, "respond_request", project_id, user_id, request_id=request_id)
    require_member(db, project_id, user_id)
    body = _validated(MessageCreate, body)
    item = _get_request(db, project_id, request_id, lock=True)
    if agent is not None:
        _authorize_agent(
            db, agent, "respond_request", project_id, user_id, request_id=request_id, lock=True
        )
    if item.accepted_by is None and user_id in _target_users(
        db, project_id, item.target_user_id, item.target_module_id
    ):
        raise CollaborationStateError("请先接手需求，再作回应")
    if item.accepted_by != user_id:
        raise CollaborationPermissionError("只有接手人可以回应需求")
    if item.status not in ("accepted", "responded"):
        raise CollaborationStateError("只有已接手且尚未解决的需求可以回应")
    message = _append_message(db, item, user_id, body, "response", agent=agent)
    item.responded_at = message.created_at
    item.status = "responded"
    return _finish_request_change(
        db,
        item,
        user_id,
        "request_responded",
        "需求已有回应",
        details={"message_id": str(message.id)},
        agent=agent,
    )


def supplement_request(
    db: Session, project_id: UUID, request_id: UUID, user_id: UUID, body: MessageCreate | dict
) -> RequestInfo:
    require_member(db, project_id, user_id)
    body = _validated(MessageCreate, body)
    item = _get_request(db, project_id, request_id, lock=True)
    if item.created_by != user_id:
        raise CollaborationPermissionError("只有发起人可以补充需求")
    if item.status == "resolved":
        raise CollaborationStateError("已解决的需求不能继续补充")
    message = _append_message(db, item, user_id, body, "supplement")
    if item.status == "responded":
        # 发起人仍需补充回应，不能沿用上一次回应直接确认解决。
        item.status = "accepted"
    return _finish_request_change(
        db,
        item,
        user_id,
        "request_supplemented",
        "需求有新的补充说明",
        details={"message_id": str(message.id)},
    )


def resolve_request(db: Session, project_id: UUID, request_id: UUID, user_id: UUID) -> RequestInfo:
    require_member(db, project_id, user_id)
    item = _get_request(db, project_id, request_id, lock=True)
    if item.created_by != user_id:
        raise CollaborationPermissionError("只有发起人可以确认需求解决")
    if item.status != "responded":
        raise CollaborationStateError("只有已回应的需求可以确认解决")
    item.resolved_at = datetime.now(UTC)
    item.status = "resolved"
    return _finish_request_change(db, item, user_id, "request_resolved", "发起人已确认解决")
