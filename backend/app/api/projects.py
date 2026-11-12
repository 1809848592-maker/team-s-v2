from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.api.auth import require_csrf, require_user
from app.db import get_session
from app.models import Project, User
from app.services import model_disclosure, projects

router = APIRouter(tags=["projects"])
Database = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(require_user)]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    data_transfer_consent: bool = Field(default=False, strict=True)
    disclosure_version: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("请输入有效的项目名称")
        return value


class ProjectInfo(BaseModel):
    id: UUID
    name: str
    created_by: UUID
    created_at: datetime
    role: projects.Role


class MemberInfo(BaseModel):
    user_id: UUID
    username: str
    role: projects.Role


class InvitationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: projects.Role


class InvitationInfo(BaseModel):
    token: str
    join_path: str


class InvitationAccept(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=128)


class InvitationAccepted(BaseModel):
    project: ProjectInfo
    membership: MemberInfo


def project_info(project: Project, role: str) -> ProjectInfo:
    return ProjectInfo(
        id=project.id,
        name=project.name,
        created_by=project.created_by,
        created_at=project.created_at,
        role=role,
    )


def project_error(error: projects.ProjectError) -> HTTPException:
    if isinstance(error, projects.ProjectNotFoundError):
        return HTTPException(404, "项目不存在或你尚未加入该项目")
    if isinstance(error, projects.LeaderRequiredError):
        return HTTPException(403, "只有项目负责人可以邀请成员")
    if isinstance(error, projects.AlreadyMemberError):
        return HTTPException(409, "你已是该项目成员，此邀请尚未使用")
    return HTTPException(404, "邀请不存在或已被使用，请向负责人索取新的邀请")


@router.post(
    "/projects", status_code=201, response_model=ProjectInfo, dependencies=[Depends(require_csrf)]
)
def create_project(body: ProjectCreate, user: CurrentUser, db: Database) -> ProjectInfo:
    try:
        project = projects.create_project(
            db,
            body.name,
            user.id,
            data_transfer_consent=body.data_transfer_consent,
            disclosure_version=body.disclosure_version,
        )
    except model_disclosure.DisclosureVersionError as error:
        raise HTTPException(409, str(error), headers={"Cache-Control": "no-store"}) from None
    return project_info(project, "leader")


@router.get("/projects", response_model=list[ProjectInfo])
def list_projects(user: CurrentUser, db: Database) -> list[ProjectInfo]:
    return [project_info(project, role) for project, role in projects.list_projects(db, user.id)]


@router.get("/projects/{project_id}", response_model=ProjectInfo)
def get_project(project_id: UUID, user: CurrentUser, db: Database) -> ProjectInfo:
    try:
        project, membership = projects.get_project(db, project_id, user.id)
    except projects.ProjectError as error:
        raise project_error(error) from None
    return project_info(project, membership.role)


@router.get("/projects/{project_id}/members", response_model=list[MemberInfo])
def list_members(project_id: UUID, user: CurrentUser, db: Database) -> list[MemberInfo]:
    try:
        members = projects.list_members(db, project_id, user.id)
    except projects.ProjectError as error:
        raise project_error(error) from None
    return [
        MemberInfo(user_id=user.id, username=user.username, role=role) for user, role in members
    ]


@router.post(
    "/projects/{project_id}/invitations",
    status_code=201,
    response_model=InvitationInfo,
    dependencies=[Depends(require_csrf)],
)
def create_invitation(
    project_id: UUID, body: InvitationCreate, user: CurrentUser, db: Database, response: Response
) -> InvitationInfo:
    try:
        raw = projects.create_invitation(db, project_id, user.id, body.role)
    except projects.ProjectError as error:
        raise project_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return InvitationInfo(token=raw, join_path=f"/?invite={raw}")


@router.post(
    "/invitations/accept",
    response_model=InvitationAccepted,
    dependencies=[Depends(require_csrf)],
)
def accept_invitation(
    body: InvitationAccept, user: CurrentUser, db: Database
) -> InvitationAccepted:
    try:
        project, membership = projects.accept_invitation(db, body.token, user.id)
    except projects.ProjectError as error:
        raise project_error(error) from None
    return InvitationAccepted(
        project=project_info(project, membership.role),
        membership=MemberInfo(user_id=user.id, username=user.username, role=membership.role),
    )
