"""明示外发范围；默认不授权，模型预算预留前必须查验项目同意。"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events import record_event
from app.models import DeliveryVersion, Project, ProjectMember
from app.models.model_disclosure import ProjectModelConsent, SubmissionDisclosure
from app.services.projects import ProjectNotFoundError, require_member

DISCLOSURE_VERSION = "deepseek-2026-11-12.4"


class DisclosureVersionError(ValueError):
    pass


class ModelConsentRequiredError(ValueError):
    def __init__(self):
        super().__init__("项目尚未完成 DeepSeek 数据外发知情同意，AI 审核未启用")


def notice() -> dict:
    return {
        "version": DISCLOSURE_VERSION,
        "provider": "DeepSeek",
        "summary": (
            "AI 审核会将交付提取文本、相关方案和规范、意见处理历史及所需上游交付摘要"
            "发送给 DeepSeek（杭州深度求索人工智能基础技术研究有限公司）。"
            "完整衔接审核会发送项目各模块已采用交付的提取文本、交接说明及摘要。"
            "方案自检会发送负责人当前编辑的未发布方案和分工草稿。"
            "辅助起草规范时，会将负责人填写的起草要求文字发送给 DeepSeek。"
            "文献核查还会向 Crossref、OpenAlex 发送参考文献的题名、作者、年份或 DOI。"
        ),
        "uncertainty": (
            "截至2026-11-12，尚未核实 DeepSeek API 请求数据明确不用于训练的承诺、"
            "API 专属保留期限或训练退出配置。请核对数据外发要求后再决定是否启用。"
        ),
        "terms_url": "https://cdn.deepseek.com/policies/zh-CN/deepseek-open-platform-terms-of-service.html",
        "privacy_url": "https://cdn.deepseek.com/policies/zh-CN/deepseek-privacy-policy.html",
        "consent_required": True,
    }


def _validate_version(version: str | None) -> None:
    if version != DISCLOSURE_VERSION:
        raise DisclosureVersionError("数据外发告知已更新或尚未确认，请刷新并阅读后重试")


def get_project_disclosure(db: Session, project_id: UUID, user_id: UUID) -> dict:
    member = require_member(db, project_id, user_id)
    consent = db.scalar(
        select(ProjectModelConsent).where(
            ProjectModelConsent.project_id == project_id,
            ProjectModelConsent.disclosure_version == DISCLOSURE_VERSION,
        )
    )
    return {
        "notice": notice(),
        "project_consent": {
            "actor_id": str(consent.actor_id),
            "accepted_at": consent.accepted_at.isoformat(),
            "disclosure_version": consent.disclosure_version,
        }
        if consent
        else None,
        "submission_acknowledged": _has_submission_ack(db, project_id, user_id),
        "can_consent": member.role == "leader",
    }


def accept_project_consent(
    db: Session, project_id: UUID, user_id: UUID, version: str, *, commit: bool = True
) -> dict:
    try:
        with db.no_autoflush:
            require_member(db, project_id, user_id, leader=True)
            if (
                db.scalar(select(Project.id).where(Project.id == project_id).with_for_update())
                is None
            ):
                raise ProjectNotFoundError
            db.get(ProjectMember, (project_id, user_id), populate_existing=True)
            require_member(db, project_id, user_id, leader=True)
            _validate_version(version)
            existing = db.scalar(
                select(ProjectModelConsent.id).where(
                    ProjectModelConsent.project_id == project_id,
                    ProjectModelConsent.disclosure_version == DISCLOSURE_VERSION,
                )
            )
            if existing is None:
                event = record_event(
                    db,
                    project_id=project_id,
                    actor_id=user_id,
                    event_type="model_data_transfer_consented",
                    object_type="project",
                    object_id=project_id,
                    details={"provider": "DeepSeek", "disclosure_version": DISCLOSURE_VERSION},
                )
                db.add(
                    ProjectModelConsent(
                        project_id=project_id,
                        actor_id=user_id,
                        disclosure_version=DISCLOSURE_VERSION,
                        notice=notice(),
                        event_id=event.id,
                        accepted_at=datetime.now(UTC),
                    )
                )
        db.flush()
        if commit:
            db.commit()
        return get_project_disclosure(db, project_id, user_id)
    except Exception:
        db.rollback()
        raise


def require_review_consent(db: Session, project_id: UUID) -> None:
    if (
        db.scalar(
            select(ProjectModelConsent.id).where(
                ProjectModelConsent.project_id == project_id,
                ProjectModelConsent.disclosure_version == DISCLOSURE_VERSION,
            )
        )
        is None
    ):
        raise ModelConsentRequiredError


def _has_submission_ack(db: Session, project_id: UUID, user_id: UUID) -> bool:
    return (
        db.scalar(
            select(SubmissionDisclosure.id).where(
                SubmissionDisclosure.project_id == project_id,
                SubmissionDisclosure.user_id == user_id,
                SubmissionDisclosure.disclosure_version == DISCLOSURE_VERSION,
            )
        )
        is not None
    )


def validate_submission_notice(
    db: Session, project_id: UUID, user_id: UUID, version: str | None
) -> None:
    if version is not None or not _has_submission_ack(db, project_id, user_id):
        _validate_version(version)


def record_submission_notice(db: Session, item: DeliveryVersion) -> None:
    # 由submit_delivery在项目锁内调用，和交付/审核任务同事务。
    if _has_submission_ack(db, item.project_id, item.submitted_by):
        return
    event = record_event(
        db,
        project_id=item.project_id,
        actor_id=item.submitted_by,
        event_type="submission_data_transfer_disclosed",
        object_type="delivery_version",
        object_id=item.id,
        task_id=item.module_id,
        plan_version_id=item.plan_version_id,
        spec_version_id=item.spec_version_id,
        details={"provider": "DeepSeek", "disclosure_version": DISCLOSURE_VERSION},
    )
    db.add(
        SubmissionDisclosure(
            project_id=item.project_id,
            user_id=item.submitted_by,
            delivery_id=item.id,
            disclosure_version=DISCLOSURE_VERSION,
            notice=notice(),
            event_id=event.id,
            acknowledged_at=datetime.now(UTC),
        )
    )
