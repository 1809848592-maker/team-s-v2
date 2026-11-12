"""负责人当前草稿的同步自检：短事务留痕，外发期间不持有DB连接。"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.events import record_event
from app.models import Module, PlanSelfCheckRequest, Project, ProjectMember
from app.review.plan_self_check import run_self_check
from app.schemas.plan_self_check import PlanSelfCheckGenerate, content_hash
from app.services import model_disclosure
from app.services.modules import ModuleValidationError
from app.services.projects import ProjectNotFoundError, require_member


class SelfCheckDuplicateError(ValueError):
    def __init__(self):
        super().__init__("本次自检请求已处理或正在处理，请勿重复发送；需要重新自检时请重新发起。")


def _authorize(db, project_id, user_id, version):
    require_member(db, project_id, user_id, leader=True)
    if db.scalar(select(Project.id).where(Project.id == project_id).with_for_update()) is None:
        raise ProjectNotFoundError
    db.get(ProjectMember, (project_id, user_id), populate_existing=True)
    require_member(db, project_id, user_id, leader=True)
    if version != model_disclosure.DISCLOSURE_VERSION:
        raise model_disclosure.DisclosureVersionError("数据外发告知已更新，请重新阅读后重试")
    model_disclosure.require_review_consent(db, project_id)


def _validate_scope(db, project_id, modules):
    ids = {m.id for m in modules}
    if any(
        m.project_id != project_id for m in db.scalars(select(Module).where(Module.id.in_(ids)))
    ):
        raise ModuleValidationError("模块标识不可用于该项目，请创建新的模块标识")
    assignees = {user_id for module in modules for user_id in module.assignee_ids}
    members = set(
        db.scalars(
            select(ProjectMember.user_id).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id.in_(assignees),
            )
        )
    )
    if assignees != members:
        raise ModuleValidationError("模块负责人必须是本项目成员")
    # 不写入稳定模块、不调用发布校验；循环或悬空依赖仍可作为待审数据。


def generate(sessions, project_id, user_id, body, *, client):
    body = PlanSelfCheckGenerate.model_validate(body)
    with sessions() as db:
        try:
            _authorize(db, project_id, user_id, body.disclosure_version)
            _validate_scope(db, project_id, body.content.modules)
            if db.get(PlanSelfCheckRequest, body.request_id) is not None:
                raise SelfCheckDuplicateError
            event = record_event(
                db,
                project_id=project_id,
                actor_id=user_id,
                event_type="plan_self_check_requested",
                object_type="plan_self_check_request",
                object_id=body.request_id,
                details={"request_id": str(body.request_id)},
            )
            db.add(
                PlanSelfCheckRequest(
                    id=body.request_id,
                    project_id=project_id,
                    requested_by=user_id,
                    content_hash=content_hash(body.content),
                    disclosure_version=body.disclosure_version,
                    event_id=event.id,
                )
            )
            db.commit()
        except IntegrityError as error:
            db.rollback()
            if getattr(error.orig, "sqlstate", None) == "23505":
                raise SelfCheckDuplicateError from None
            raise
        except Exception:
            db.rollback()
            raise
    result = run_self_check(client, project_id=project_id, body=body)
    with sessions() as db:
        try:
            _authorize(db, project_id, user_id, body.disclosure_version)
            record_event(
                db,
                project_id=project_id,
                actor_type="agent",
                actor_id="platform-plan-self-checker",
                event_type="plan_self_check_finished",
                object_type="plan_self_check_request",
                object_id=body.request_id,
                details={
                    "execution_origin": "platform_plan_self_check",
                    "initiated_by": str(user_id),
                    "request_id": str(body.request_id),
                    "status": result["status"],
                    "finding_count": len(result["findings"]),
                    "instruction_version": result["instruction_version"],
                },
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
    return result
