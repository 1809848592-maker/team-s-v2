from uuid import UUID

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.events import record_event
from app.models.agent import Agent
from app.models.delivery import DeliveryVersion
from app.models.plan import PlanVersion
from app.models.project import Project
from app.models.spec import SpecVersion
from app.schemas.agent_context import (
    DataBlock,
    DependencyContext,
    PlanContext,
    PlanOverview,
    ReviewCoverage,
    TaskContext,
    TaskDependencies,
    UpstreamHandoff,
)
from app.schemas.plans import ModuleContent, PlanContent
from app.schemas.specs import SpecVersionInfo
from app.services import agent_permissions


def _dependencies(task: ModuleContent, modules: list[ModuleContent]) -> TaskDependencies:
    by_id = {module.id: module for module in modules}
    return TaskDependencies(
        upstream=[
            DependencyContext(module=by_id[dependency.module_id], needs=dependency.needs)
            for dependency in task.upstream
        ],
        downstream=[
            DependencyContext(module=module, needs=dependency.needs)
            for module in modules
            for dependency in module.upstream
            if dependency.module_id == task.id
        ],
    )


def _upstream_handoffs(
    db: Session, agent: Agent, project_id: UUID, task: ModuleContent
) -> list[DataBlock[UpstreamHandoff]]:
    module_ids = list(dict.fromkeys(dependency.module_id for dependency in task.upstream))
    if not module_ids:
        return []
    candidates = db.scalars(
        select(DeliveryVersion)
        .where(DeliveryVersion.project_id == project_id, DeliveryVersion.module_id.in_(module_ids))
        .order_by(DeliveryVersion.module_id, DeliveryVersion.version.desc())
        .execution_options(populate_existing=True)
    )
    selected: dict[UUID, DataBlock[UpstreamHandoff]] = {}
    for delivery in candidates:
        if delivery.module_id in selected:
            continue
        if not agent_permissions.delivery_is_adopted(db, delivery):
            continue
        # 只有本任务的直接上游已采用版本才进入返回；统一权限再核验对象范围。
        agent_permissions.authorize(
            db,
            agent,
            "read_delivery",
            project_id=project_id,
            delivery_id=delivery.id,
        )
        selected[delivery.module_id] = DataBlock(
            content=UpstreamHandoff(
                id=delivery.id,
                project_id=delivery.project_id,
                module_id=delivery.module_id,
                version=delivery.version,
                submitted_by=delivery.submitted_by,
                submitted_at=delivery.submitted_at,
                plan_version_id=delivery.plan_version_id,
                spec_version_id=delivery.spec_version_id,
                filename=delivery.filename,
                handoff_note=delivery.handoff_note,
            )
        )
    return [selected[module_id] for module_id in module_ids if module_id in selected]


def get_task_context(
    db: Session,
    agent: Agent,
    module_id: UUID,
    *,
    project_id: UUID | None = None,
) -> TaskContext:
    """返回本人的任务资料；所有正文留在标注“数据，不是指令”的结构化边界中。"""
    with db.no_autoflush:
        agent_permissions.authorize(
            db, agent, "read_task_context", project_id=project_id, module_id=module_id
        )
        agent_identity = inspect(agent).identity
        agent_id = agent_identity[0] if agent_identity is not None else agent.id
        principal = db.execute(
            select(Agent.project_id, Agent.owner_id).where(Agent.id == agent_id)
        ).one()
        # 方案与规范发布持项目排他锁；共享锁让本次读取固定于同一发布时点。
        db.scalar(
            select(Project.id).where(Project.id == principal.project_id).with_for_update(read=True)
        )
        agent_permissions.authorize(
            db,
            agent,
            "read_task_context",
            project_id=principal.project_id,
            module_id=module_id,
        )
        plan = db.scalar(
            select(PlanVersion)
            .where(PlanVersion.project_id == principal.project_id)
            .order_by(PlanVersion.version.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        content = PlanContent.model_validate(plan.content)
        task = next(module for module in content.modules if module.id == module_id)
        spec = db.scalar(
            select(SpecVersion)
            .where(SpecVersion.project_id == principal.project_id)
            .order_by(SpecVersion.version.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        handoffs = _upstream_handoffs(db, agent, principal.project_id, task)
        result = TaskContext(
            project_id=principal.project_id,
            task_id=module_id,
            represented_user_id=principal.owner_id,
            plan_version_id=plan.id,
            spec_version_id=spec.id if spec is not None else None,
            spec_status="published" if spec is not None else "unpublished",
            task=DataBlock(content=task),
            plan=DataBlock(
                content=PlanContext(
                    id=plan.id,
                    project_id=plan.project_id,
                    version=plan.version,
                    change_note=plan.change_note,
                    published_by=plan.published_by,
                    published_at=plan.published_at,
                    content=PlanOverview.model_validate(content.model_dump(exclude={"modules"})),
                )
            ),
            spec=(
                DataBlock(content=SpecVersionInfo.model_validate(spec))
                if spec is not None
                else None
            ),
            dependencies=DataBlock(content=_dependencies(task, content.modules)),
            upstream_deliveries=handoffs,
            latest_review=DataBlock(content=ReviewCoverage()),
        )
        record_event(
            db,
            project_id=principal.project_id,
            actor_type="agent",
            actor_id=agent_id,
            represented_user_id=principal.owner_id,
            event_type="agent_task_context_read",
            object_type="module",
            object_id=module_id,
            task_id=module_id,
            plan_version_id=plan.id,
            spec_version_id=spec.id if spec is not None else None,
            details={
                "plan_version": plan.version,
                "spec_version": spec.version if spec is not None else None,
                "upstream_delivery_ids": [str(item.content.id) for item in handoffs],
                "upstream_delivery_count": len(handoffs),
                "review_coverage": "not_covered",
            },
        )
        db.commit()
        return result
