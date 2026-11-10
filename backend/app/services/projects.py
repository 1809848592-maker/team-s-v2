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


def create_project(db: Session, name: str, user_id: UUID) -> Project:
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
    # PostgreSQL 行锁让并发使用同一邀请的请求依次检查最新的使用状态。
    invitation = db.scalar(
        select(Invitation).where(Invitation.token_hash == token_hash(raw)).with_for_update()
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
    project = db.get(Project, invitation.project_id)
    if project is None:
        raise ProjectNotFoundError
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
