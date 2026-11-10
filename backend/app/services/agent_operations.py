"""供 HTTP 和 MCP 复用的智能体只读操作；正文始终封装为资料数据。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events import record_event
from app.models.agent import Agent
from app.models.collaboration import CollaborationRequest
from app.models.delivery import DeliveryVersion
from app.models.project import Project
from app.schemas.agent_context import DataBlock, ReviewCoverage
from app.schemas.deliveries import DeliveryVersionInfo
from app.schemas.plans import ModuleInfo, PlanContent, PlanVersionInfo
from app.schemas.specs import SpecVersionInfo
from app.services import agent_permissions as permissions
from app.services import collaboration, plans, resources, specs
from app.storage import Storage


def _begin(db: Session, agent: Agent, action: str, **scope) -> None:
    permissions.authorize(db, agent, action, **scope)
    db.scalar(select(Project.id).where(Project.id == agent.project_id).with_for_update(read=True))
    permissions.authorize(db, agent, action, **scope)


def _finish(db: Session, agent: Agent, event_type: str, content, **metadata):
    record_event(
        db,
        project_id=agent.project_id,
        actor_type="agent",
        actor_id=agent.id,
        represented_user_id=agent.owner_id,
        event_type=event_type,
        object_type=metadata.pop("object_type", "project"),
        object_id=metadata.pop("object_id", agent.project_id),
        **metadata,
    )
    db.commit()
    return DataBlock(content=content)


def get_my_tasks(db: Session, agent: Agent, *, project_id: UUID | None = None):
    _begin(db, agent, "read_tasks", project_id=project_id)
    plan = plans.get_latest_plan(db, agent.project_id)
    tasks = (
        [
            ModuleInfo(**item.model_dump(), plan_version_id=plan.id)
            for item in PlanContent.model_validate(plan.content).modules
            if agent.owner_id in item.assignee_ids
        ]
        if plan
        else []
    )
    return _finish(
        db,
        agent,
        "agent_tasks_read",
        tasks,
        plan_version_id=plan.id if plan else None,
        details={"module_ids": [str(item.id) for item in tasks]},
    )


def get_plan(db: Session, agent: Agent, *, project_id: UUID | None = None):
    _begin(db, agent, "read_plan", project_id=project_id)
    plan = plans.get_latest_plan(db, agent.project_id)
    if plan is None:
        raise plans.PlanNotFoundError
    return _finish(
        db,
        agent,
        "agent_plan_read",
        PlanVersionInfo.model_validate(plan),
        object_type="plan_version",
        object_id=plan.id,
        plan_version_id=plan.id,
        details={"version": plan.version},
    )


def get_spec(db: Session, agent: Agent, *, project_id: UUID | None = None):
    _begin(db, agent, "read_spec", project_id=project_id)
    spec = specs.get_latest_spec(db, agent.project_id)
    if spec is None:
        raise specs.SpecNotFoundError
    return _finish(
        db,
        agent,
        "agent_spec_read",
        SpecVersionInfo.model_validate(spec),
        object_type="spec_version",
        object_id=spec.id,
        spec_version_id=spec.id,
        details={"version": spec.version},
    )


def _delivery_info(item: DeliveryVersion) -> DeliveryVersionInfo:
    return DeliveryVersionInfo.model_validate(item).model_copy(
        update={"download_url": f"/api/agent/deliveries/{item.id}/download"}
    )


def list_deliveries(
    db: Session, agent: Agent, *, module_id: UUID | None = None, project_id: UUID | None = None
):
    _begin(db, agent, "read_tasks", project_id=project_id)
    plan = plans.get_latest_plan(db, agent.project_id)
    modules = PlanContent.model_validate(plan.content).modules if plan else []
    own_modules = {item.id for item in modules if agent.owner_id in item.assignee_ids}
    upstream_ids = {
        dependency.module_id
        for item in modules
        if item.id in own_modules
        for dependency in item.upstream
    }
    query = select(DeliveryVersion).where(DeliveryVersion.project_id == agent.project_id)
    if module_id is not None:
        query = query.where(DeliveryVersion.module_id == module_id)
    candidates = list(db.scalars(query.order_by(DeliveryVersion.submitted_at.desc())))
    if (
        module_id is not None
        and module_id not in own_modules | upstream_ids
        and not any(item.submitted_by == agent.owner_id for item in candidates)
    ):
        permissions.deny(db, agent, "read_delivery", "module_scope", module_id=module_id)
    visible = [
        item
        for item in candidates
        if item.submitted_by == agent.owner_id
        or (item.module_id in upstream_ids and permissions.delivery_is_adopted(db, item))
    ]
    return _finish(
        db,
        agent,
        "agent_deliveries_read",
        [_delivery_info(item) for item in visible],
        task_id=module_id,
        details={"delivery_ids": [str(item.id) for item in visible]},
    )


def get_delivery(db: Session, agent: Agent, delivery_id: UUID, *, project_id: UUID | None = None):
    _begin(db, agent, "read_delivery", project_id=project_id, delivery_id=delivery_id)
    item = db.get(DeliveryVersion, delivery_id)
    return _finish(
        db,
        agent,
        "agent_delivery_read",
        _delivery_info(item),
        object_type="delivery_version",
        object_id=item.id,
        task_id=item.module_id,
        plan_version_id=item.plan_version_id,
        spec_version_id=item.spec_version_id,
    )


def download_delivery(
    db: Session,
    agent: Agent,
    delivery_id: UUID,
    *,
    storage: Storage,
    project_id: UUID | None = None,
):
    _begin(db, agent, "read_delivery", project_id=project_id, delivery_id=delivery_id)
    item = db.get(DeliveryVersion, delivery_id)
    path = storage.path(item.sha256)
    if not path.is_file():
        raise FileNotFoundError
    _finish(
        db,
        agent,
        "agent_delivery_downloaded",
        None,
        object_type="delivery_version",
        object_id=item.id,
        task_id=item.module_id,
        plan_version_id=item.plan_version_id,
        spec_version_id=item.spec_version_id,
    )
    return path, item.filename


def get_review_result(
    db: Session, agent: Agent, delivery_id: UUID, *, project_id: UUID | None = None
):
    _begin(db, agent, "read_review", project_id=project_id, delivery_id=delivery_id)
    item = db.get(DeliveryVersion, delivery_id)
    return _finish(
        db,
        agent,
        "agent_review_read",
        ReviewCoverage(),
        object_type="delivery_version",
        object_id=item.id,
        task_id=item.module_id,
        plan_version_id=item.plan_version_id,
        spec_version_id=item.spec_version_id,
        details={"coverage": "not_covered"},
    )


def list_requests(db: Session, agent: Agent, *, project_id: UUID | None = None):
    _begin(db, agent, "read_requests", project_id=project_id)
    plan = plans.get_latest_plan(db, agent.project_id)
    own_modules = (
        {
            item.id
            for item in PlanContent.model_validate(plan.content).modules
            if agent.owner_id in item.assignee_ids
        }
        if plan
        else set()
    )
    items = list(
        db.scalars(
            select(CollaborationRequest)
            .where(
                CollaborationRequest.project_id == agent.project_id,
                (CollaborationRequest.created_by == agent.owner_id)
                | (CollaborationRequest.target_user_id == agent.owner_id)
                | (CollaborationRequest.accepted_by == agent.owner_id)
                | CollaborationRequest.target_module_id.in_(own_modules),
            )
            .order_by(CollaborationRequest.created_at.desc())
        )
    )
    result = collaboration._request_infos(db, items)
    return _finish(
        db,
        agent,
        "agent_requests_read",
        result,
        details={"request_ids": [str(item.id) for item in items]},
    )


def get_request(db: Session, agent: Agent, request_id: UUID, *, project_id: UUID | None = None):
    _begin(db, agent, "read_requests", project_id=project_id, request_id=request_id)
    item = db.get(CollaborationRequest, request_id)
    return _finish(
        db,
        agent,
        "agent_request_read",
        collaboration._request_infos(db, [item])[0],
        object_type="collaboration_request",
        object_id=item.id,
        task_id=item.target_module_id,
    )


def _resource_info(item):
    return {
        "id": str(item.id),
        "filename": item.filename,
        "size": item.size,
        "sha256": item.sha256,
        "builtin": item.builtin,
        "download_url": f"/api/agent/resources/{item.id}/download",
    }


def list_resources(db: Session, agent: Agent, *, storage: Storage, project_id: UUID | None = None):
    _begin(db, agent, "read_resources", project_id=project_id)
    items = resources.list_resources(db, agent.project_id, agent.owner_id, storage=storage)
    return _finish(
        db,
        agent,
        "agent_resources_read",
        [_resource_info(item) for item in items],
        details={"resource_ids": [str(item.id) for item in items]},
    )


def download_resource(
    db: Session,
    agent: Agent,
    resource_id: UUID,
    *,
    storage: Storage,
    project_id: UUID | None = None,
):
    _begin(db, agent, "read_resources", project_id=project_id)
    try:
        item = resources.resolve_resource(
            db, agent.project_id, agent.owner_id, resource_id, storage=storage
        )
    except resources.ResourceNotFoundError:
        permissions.deny(db, agent, "read_resources", "resource_scope")
    if not item.path.is_file():
        raise FileNotFoundError
    _finish(
        db,
        agent,
        "agent_resource_downloaded",
        None,
        object_type="resource",
        object_id=item.id,
    )
    return item.path, item.filename
