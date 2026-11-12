"""负责人单次起草：短事务授权留痕，释放连接后调用，结果只返回编辑区。"""

import hashlib
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.events import record_event
from app.models import Project, ProjectMember, SpecDraftRequest
from app.review.spec_draft import generate_spec_draft
from app.schemas.spec_drafting import SpecDraftGenerate
from app.services import model_disclosure
from app.services.projects import ProjectNotFoundError, require_member


class SpecDraftDuplicateError(ValueError):
    def __init__(self):
        super().__init__("本次起草请求已处理或正在处理，请勿重复发送；需要重新生成时请重新发起。")


def _authorize(db, project_id, user_id, version):
    require_member(db, project_id, user_id, leader=True)
    if db.scalar(select(Project.id).where(Project.id == project_id).with_for_update()) is None:
        raise ProjectNotFoundError
    db.get(ProjectMember, (project_id, user_id), populate_existing=True)
    require_member(db, project_id, user_id, leader=True)
    if version != model_disclosure.DISCLOSURE_VERSION:
        raise model_disclosure.DisclosureVersionError("数据外发告知已更新，请重新阅读后重试")
    model_disclosure.require_review_consent(db, project_id)


def generate_draft(sessions, project_id: UUID, user_id: UUID, body, *, client) -> dict:
    body = SpecDraftGenerate.model_validate(body)
    with sessions() as db:
        try:
            _authorize(db, project_id, user_id, body.disclosure_version)
            if db.get(SpecDraftRequest, body.request_id) is not None:
                raise SpecDraftDuplicateError
            event = record_event(
                db,
                project_id=project_id,
                actor_id=user_id,
                event_type="spec_draft_requested",
                object_type="spec_draft_request",
                object_id=body.request_id,
                details={"request_id": str(body.request_id)},
            )
            db.add(
                SpecDraftRequest(
                    id=body.request_id,
                    project_id=project_id,
                    requested_by=user_id,
                    input_hash=hashlib.sha256(body.description.encode("utf-8")).hexdigest(),
                    disclosure_version=body.disclosure_version,
                    event_id=event.id,
                )
            )
            db.commit()
        except IntegrityError as error:
            db.rollback()
            if getattr(error.orig, "sqlstate", None) == "23505":
                raise SpecDraftDuplicateError from None
            raise
        except Exception:
            db.rollback()
            raise
    # 不持有项目锁或数据库连接；只有固定生成器能选择指令/模型来源。
    result = generate_spec_draft(
        client, project_id=project_id, request_id=body.request_id, description=body.description
    )
    with sessions() as db:
        try:
            # 迟到结果不能交给已失去负责人身份的人；真实用量已独立结算，不撤销。
            _authorize(db, project_id, user_id, body.disclosure_version)
            record_event(
                db,
                project_id=project_id,
                actor_type="agent",
                actor_id="platform-spec-drafter",
                event_type="spec_draft_generated",
                object_type="spec_draft_request",
                object_id=body.request_id,
                details={
                    "execution_origin": "platform_spec_draft",
                    "initiated_by": str(user_id),
                    "request_id": str(body.request_id),
                    "call_id": str(body.request_id),
                    "instruction_version": result["instruction_version"],
                    "item_count": len(result["items"]),
                },
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {**result, "request_id": body.request_id, "call_id": body.request_id}
