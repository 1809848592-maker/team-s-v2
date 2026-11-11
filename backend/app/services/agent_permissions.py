"""智能体能力白名单及项目、所属人和对象范围检查。"""

from typing import NoReturn
from uuid import UUID

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.events import record_event
from app.models.agent import Agent
from app.models.collaboration import CollaborationRequest
from app.models.delivery import DeliveryVersion
from app.models.plan import PlanVersion
from app.models.project import ProjectMember
from app.models.review_workflow import DeliveryWorkflow
from app.schemas.plans import ModuleContent, PlanContent

_PROJECT_ACTIONS = frozenset(
    {"read_project", "read_plan", "read_modules", "read_spec", "read_resources", "create_request"}
)
_MODULE_ACTIONS = frozenset({"read_tasks", "read_task_context", "report_progress", "upload_draft"})
_DELIVERY_ACTIONS = frozenset({"read_delivery", "read_review"})
_REQUEST_ACTIONS = frozenset({"read_requests", "accept_request", "respond_request"})
_ALLOWED_ACTIONS = _PROJECT_ACTIONS | _MODULE_ACTIONS | _DELIVERY_ACTIONS | _REQUEST_ACTIONS


class AgentPermissionDeniedError(Exception):
    def __init__(self, action: str, reason: str):
        self.action = action
        self.reason = reason
        super().__init__(f"智能体无权执行此操作：{action}（{reason}）")


def _agent_id(agent: Agent) -> UUID:
    # 持久化身份不采用调用方尚未提交的ORM主键改动。
    identity = inspect(agent).identity
    return identity[0] if identity is not None else agent.id


def deny(
    db: Session,
    agent: Agent,
    action: str,
    reason: str,
    *,
    project_id: UUID | None = None,
    module_id: UUID | None = None,
    delivery_id: UUID | None = None,
    request_id: UUID | None = None,
) -> NoReturn:
    agent_id = _agent_id(agent)
    # 不让拒绝日志的提交顺带提交未授权的业务写入；日志使用独立的新事务保存。
    db.rollback()
    identity = db.execute(
        select(Agent.project_id, Agent.owner_id).where(Agent.id == agent_id)
    ).one_or_none()
    if identity is not None:
        details = {"action": action, "reason": reason}
        for key, value in (
            ("requested_project_id", project_id),
            ("module_id", module_id),
            ("object_id", delivery_id or request_id or module_id),
        ):
            if value is not None:
                details[key] = str(value)
        record_event(
            db,
            project_id=identity.project_id,
            actor_type="agent",
            actor_id=agent_id,
            represented_user_id=identity.owner_id,
            event_type="agent_permission_denied",
            object_type="agent",
            object_id=agent_id,
            task_id=module_id,
            details=details,
        )
        db.commit()
    raise AgentPermissionDeniedError(action, reason)


def delivery_is_adopted(db: Session, delivery: DeliveryVersion) -> bool:
    """从持久审核状态读取；无状态的历史交付从不被视为已采用。"""
    return (
        db.scalar(select(DeliveryWorkflow.state).where(DeliveryWorkflow.delivery_id == delivery.id))
        == "adopted"
    )


def _current_modules(db: Session, project_id: UUID) -> list[ModuleContent]:
    content = db.scalar(
        select(PlanVersion.content)
        .where(PlanVersion.project_id == project_id)
        .order_by(PlanVersion.version.desc())
        .limit(1)
    )
    return PlanContent.model_validate(content).modules if content is not None else []


def authorize(
    db: Session,
    agent: Agent,
    action: str,
    *,
    project_id: UUID | None = None,
    module_id: UUID | None = None,
    delivery_id: UUID | None = None,
    request_id: UUID | None = None,
) -> None:
    def reject(reason: str) -> NoReturn:
        deny(
            db,
            agent,
            action,
            reason,
            project_id=project_id,
            module_id=module_id,
            delivery_id=delivery_id,
            request_id=request_id,
        )

    # 校验期间不自动flush调用方待提交的业务；只读DB列不受ORM身份缓存或待修改字段影响。
    with db.no_autoflush:
        identity = db.execute(
            select(
                Agent.project_id,
                Agent.owner_id,
                Agent.revoked_at,
                ProjectMember.user_id.label("member_id"),
            )
            .outerjoin(
                ProjectMember,
                (ProjectMember.project_id == Agent.project_id)
                & (ProjectMember.user_id == Agent.owner_id),
            )
            .where(Agent.id == _agent_id(agent))
        ).one_or_none()
        if identity is None or identity.revoked_at is not None or identity.member_id is None:
            reject("agent_inactive")
        if project_id is not None and project_id != identity.project_id:
            reject("project_scope")
        if action not in _ALLOWED_ACTIONS:
            reject("action_forbidden")
        if action in _PROJECT_ACTIONS:
            if module_id is not None or delivery_id is not None or request_id is not None:
                reject("unexpected_object_context")
            return

        if action in _MODULE_ACTIONS:
            if delivery_id is not None or request_id is not None:
                reject("unexpected_object_context")
            if module_id is None:
                if action == "read_tasks":
                    # 列表调用方仍须按所属人过滤；带具体模块时必须通过下面的归属检查。
                    return
                reject("module_required")
            module = next(
                (
                    item
                    for item in _current_modules(db, identity.project_id)
                    if item.id == module_id
                ),
                None,
            )
            if module is None or identity.owner_id not in module.assignee_ids:
                reject("module_scope")
            return

        if action in _DELIVERY_ACTIONS:
            if request_id is not None:
                reject("unexpected_object_context")
            if delivery_id is None:
                reject("delivery_required")
            delivery = db.scalar(
                select(DeliveryVersion)
                .where(
                    DeliveryVersion.id == delivery_id,
                    DeliveryVersion.project_id == identity.project_id,
                )
                .execution_options(populate_existing=True)
            )
            if delivery is None or (module_id is not None and module_id != delivery.module_id):
                reject("delivery_scope")
            if delivery.submitted_by == identity.owner_id:
                return
            if action == "read_review":
                reject("review_scope")
            upstream_ids = {
                dependency.module_id
                for module in _current_modules(db, identity.project_id)
                if identity.owner_id in module.assignee_ids
                for dependency in module.upstream
            }
            if delivery.module_id not in upstream_ids:
                reject("dependency_scope")
            if not delivery_is_adopted(db, delivery):
                reject("delivery_not_adopted")
            return

        if delivery_id is not None:
            reject("unexpected_object_context")
        if request_id is None:
            if action == "read_requests" and module_id is None:
                # 列表调用方仅返回本人发起、被指定或已接手的需求。
                return
            reject("request_required")
        request = db.scalar(
            select(CollaborationRequest)
            .where(
                CollaborationRequest.id == request_id,
                CollaborationRequest.project_id == identity.project_id,
            )
            .execution_options(populate_existing=True)
        )
        if request is None or (module_id is not None and module_id != request.target_module_id):
            reject("request_scope")
        assigned = request.target_user_id == identity.owner_id
        if request.target_module_id is not None:
            assigned = any(
                module.id == request.target_module_id and identity.owner_id in module.assignee_ids
                for module in _current_modules(db, identity.project_id)
            )
        if action == "read_requests":
            if not assigned and identity.owner_id not in (request.created_by, request.accepted_by):
                reject("request_scope")
        elif action == "accept_request":
            if not assigned:
                reject("request_scope")
            if request.status != "open":
                reject("request_state")
        elif action == "respond_request":
            if request.accepted_by != identity.owner_id:
                reject("request_scope")
            if request.status not in ("accepted", "responded"):
                reject("request_state")
