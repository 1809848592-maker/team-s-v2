from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.events import record_event
from app.models import Invitation, Project, ProjectMember, User
from app.security import new_token, token_hash

Role = Literal["leader", "member"]


class ProjectError(Exception):
    pass


class ProjectNotFoundError(ProjectError):
    pass


class LeaderRequiredError(ProjectError):
    pass


class InvalidInvitationError(ProjectError):
    pass


class AlreadyMemberError(ProjectError):
    pass


def require_member(
    db: Session, project_id: UUID, user_id: UUID, *, leader: bool = False
) -> ProjectMember:
    membership = db.get(ProjectMember, (project_id, user_id))
    if membership is None:
        # 无论项目不存在还是用户不属于该项目，都不暴露项目是否存在。
        raise ProjectNotFoundError
    if leader and membership.role != "leader":
        raise LeaderRequiredError
    return membership


def create_project(
    db: Session,
    name: str,
    user_id: UUID,
    *,
    data_transfer_consent: bool = False,
    disclosure_version: str | None = None,
) -> Project:
    project = Project(name=name, created_by=user_id)
    db.add(project)
    db.flush()
    db.add(ProjectMember(project_id=project.id, user_id=user_id, role="leader"))
    record_event(
        db,
        project_id=project.id,
        actor_id=user_id,
        event_type="project_created",
        object_type="project",
        object_id=project.id,
        details={"name": project.name, "role": "leader"},
    )
    if data_transfer_consent:
        from app.services.model_disclosure import accept_project_consent

        db.flush()
        accept_project_consent(db, project.id, user_id, disclosure_version, commit=False)
    db.commit()
    return project


def list_projects(db: Session, user_id: UUID) -> list[tuple[Project, str]]:
    rows = db.execute(
        select(Project, ProjectMember.role)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == user_id)
        .order_by(Project.created_at.desc(), Project.id)
    )
    return [(project, role) for project, role in rows]


def get_project(db: Session, project_id: UUID, user_id: UUID) -> tuple[Project, ProjectMember]:
    membership = require_member(db, project_id, user_id)
    project = db.get(Project, project_id)
    if project is None:
        raise ProjectNotFoundError
    return project, membership


def list_members(db: Session, project_id: UUID, user_id: UUID) -> list[tuple[User, str]]:
    require_member(db, project_id, user_id)
    rows = db.execute(
        select(User, ProjectMember.role)
        .join(ProjectMember, ProjectMember.user_id == User.id)
        .where(ProjectMember.project_id == project_id)
        .order_by(ProjectMember.role, User.username_key, User.id)
    )
    return [(user, role) for user, role in rows]


def create_invitation(db: Session, project_id: UUID, user_id: UUID, role: Role) -> str:
    require_member(db, project_id, user_id, leader=True)
    raw = new_token()
    db.add(
        Invitation(token_hash=token_hash(raw), project_id=project_id, role=role, created_by=user_id)
    )
    record_event(
        db,
        project_id=project_id,
        actor_id=user_id,
        event_type="invitation_created",
        object_type="invitation",
        object_id=token_hash(raw),
        details={"role": role},
    )
    db.commit()
    return raw


def accept_invitation(db: Session, raw: str, user_id: UUID) -> tuple[Project, ProjectMember]:
    # 先定位项目，再按项目→邀请加锁；负责人加入与唯一负责人自审共享项目锁。
    # 第一次读取仅作定位，等锁后必须重读邀请，不能沿用等待前的使用状态。
    digest = token_hash(raw)
    project_id = db.scalar(select(Invitation.project_id).where(Invitation.token_hash == digest))
    if project_id is None:
        raise InvalidInvitationError
    project = db.scalar(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise InvalidInvitationError
    invitation = db.scalar(
        select(Invitation)
        .where(Invitation.token_hash == digest, Invitation.project_id == project.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if invitation is None or invitation.used_at is not None:
        raise InvalidInvitationError
    if db.get(ProjectMember, (invitation.project_id, user_id)) is not None:
        raise AlreadyMemberError

    membership = ProjectMember(
        project_id=invitation.project_id, user_id=user_id, role=invitation.role
    )
    db.add(membership)
    invitation.used_by = user_id
    invitation.used_at = datetime.now(UTC)
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        # 同一个人同时使用不同邀请时，复合主键仍阻止重复加入；回滚保留未使用的邀请。
        if getattr(error.orig, "sqlstate", None) == "23505":
            raise AlreadyMemberError from error
        raise
    record_event(
        db,
        project_id=project.id,
        actor_id=user_id,
        event_type="member_joined",
        object_type="project_member",
        object_id=user_id,
        details={"role": membership.role, "invited_by": str(invitation.created_by)},
    )
    db.commit()
    return project, membership
