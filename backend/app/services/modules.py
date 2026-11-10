from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Module, PlanVersion, ProjectMember
from app.schemas.plans import ModuleContent, ModuleInfo, PlanContent
from app.services.projects import require_member


class ModuleValidationError(ValueError):
    pass


class ModuleNotFoundError(Exception):
    pass


def validate_modules(db: Session, project_id: UUID, modules: list[ModuleContent]) -> None:
    """持项目发布锁调用；全部校验通过后才建立稳定标识，业务事务统一提交。"""
    by_id = {item.id: item for item in modules}
    if len(by_id) != len(modules):
        raise ModuleValidationError("分工模块的标识不能重复")
    ids = set(by_id)
    existing = list(db.scalars(select(Module).where(Module.id.in_(ids)))) if ids else []
    if any(item.project_id != project_id for item in existing):
        raise ModuleValidationError("模块标识不可用于该项目，请创建新的模块标识")

    assignees = {user_id for item in modules for user_id in item.assignee_ids}
    members = (
        set(
            db.scalars(
                select(ProjectMember.user_id).where(
                    ProjectMember.project_id == project_id, ProjectMember.user_id.in_(assignees)
                )
            )
        )
        if assignees
        else set()
    )
    if assignees != members:
        raise ModuleValidationError("模块负责人必须是本项目成员")
    graph: dict[UUID, set[UUID]] = {}
    for item in modules:
        dependencies = {entry.module_id for entry in item.upstream}
        if not dependencies <= ids:
            raise ModuleValidationError("上游依赖必须指向本次方案中的模块")
        graph[item.id] = dependencies
    visiting: set[UUID] = set()
    visited: set[UUID] = set()

    def visit(module_id: UUID) -> None:
        if module_id in visiting:
            raise ModuleValidationError("模块依赖不能形成环")
        if module_id in visited:
            return
        visiting.add(module_id)
        for dependency in graph[module_id]:
            visit(dependency)
        visiting.remove(module_id)
        visited.add(module_id)

    for module_id in ids:
        visit(module_id)
    known_ids = {item.id for item in existing}
    db.add_all(Module(id=module_id, project_id=project_id) for module_id in ids - known_ids)
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        if getattr(error.orig, "sqlstate", None) == "23505":
            raise ModuleValidationError("模块标识已被使用，请创建新的模块标识") from error
        raise


def _latest_plan(db: Session, project_id: UUID) -> PlanVersion | None:
    return db.scalar(
        select(PlanVersion)
        .where(PlanVersion.project_id == project_id)
        .order_by(PlanVersion.version.desc())
        .limit(1)
    )


def _current_modules(plan: PlanVersion | None) -> list[ModuleInfo]:
    if plan is None:
        return []
    content = PlanContent.model_validate(plan.content)
    return [ModuleInfo(**item.model_dump(), plan_version_id=plan.id) for item in content.modules]


def get_current_module(db: Session, project_id: UUID, module_id: UUID) -> ModuleInfo | None:
    """仅内部调用；公开读取必须先检查项目成员身份。"""
    return next(
        (item for item in _current_modules(_latest_plan(db, project_id)) if item.id == module_id),
        None,
    )


def list_modules(db: Session, project_id: UUID, user_id: UUID) -> list[ModuleInfo]:
    require_member(db, project_id, user_id)
    return _current_modules(_latest_plan(db, project_id))


def get_module(db: Session, project_id: UUID, module_id: UUID, user_id: UUID) -> ModuleInfo:
    require_member(db, project_id, user_id)
    item = get_current_module(db, project_id, module_id)
    if item is None:
        raise ModuleNotFoundError
    return item


def _comparable(module: ModuleContent) -> dict:
    content = module.model_dump(mode="json")
    content["assignee_ids"] = sorted(content["assignee_ids"])
    content["upstream"] = sorted(
        content["upstream"], key=lambda item: (item["module_id"], item["needs"])
    )
    return content


def affected_modules(
    previous: PlanVersion | None, current: PlanContent
) -> list[tuple[ModuleContent | None, ModuleContent | None]]:
    old_content = PlanContent.model_validate(previous.content) if previous is not None else None
    old_modules = {item.id: item for item in old_content.modules} if old_content is not None else {}
    new_modules = {item.id: item for item in current.modules}
    overall_changed = old_content is None or old_content.model_dump(
        mode="json", exclude={"modules"}
    ) != current.model_dump(mode="json", exclude={"modules"})
    affected = []
    for module_id in sorted(old_modules.keys() | new_modules.keys(), key=str):
        old, new = old_modules.get(module_id), new_modules.get(module_id)
        if overall_changed or old is None or new is None or _comparable(old) != _comparable(new):
            affected.append((old, new))
    return affected
