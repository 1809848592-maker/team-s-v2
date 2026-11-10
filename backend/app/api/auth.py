from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import User
from app.services import auth

router = APIRouter(prefix="/auth", tags=["auth"])
Database = Annotated[Session, Depends(get_session)]


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=256)

    @field_validator("username")
    @classmethod
    def clean_username(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("请输入有效的用户名或邮箱")
        return value


class Account(BaseModel):
    id: UUID
    username: str
    is_admin: bool


def require_csrf(request: Request) -> None:
    # 自定义头阻止跨站表单提交；本应用不开放跨域凭据请求。
    origin = request.headers.get("origin")
    if request.headers.get("x-csrf-protection") != "1" or (
        origin is not None and origin != str(request.base_url).rstrip("/")
    ):
        raise HTTPException(403, "请求来源无法验证，请刷新页面后重试")


def require_user(request: Request, db: Database) -> User:
    try:
        return auth.current_user(db, request.cookies.get(auth.COOKIE_NAME))
    except auth.UnauthenticatedError:
        raise HTTPException(401, "请先登录，或重新登录已过期的账号") from None


def account(user: User, request: Request) -> Account:
    return Account(
        id=user.id,
        username=user.username,
        is_admin=str(user.id) in request.app.state.settings.admin_user_ids,
    )


def set_session_cookie(response: Response, raw: str, request: Request) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        auth.COOKIE_NAME,
        raw,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


@router.post(
    "/register", status_code=201, response_model=Account, dependencies=[Depends(require_csrf)]
)
def register(body: Credentials, request: Request, response: Response, db: Database) -> Account:
    try:
        user, raw = auth.register(
            db,
            body.username,
            body.password,
            request.app.state.settings,
            request.cookies.get(auth.COOKIE_NAME),
        )
    except auth.UsernameTakenError:
        raise HTTPException(409, "该账号已注册，请直接登录或换一个账号") from None
    set_session_cookie(response, raw, request)
    return account(user, request)


@router.post("/login", response_model=Account, dependencies=[Depends(require_csrf)])
def login(body: Credentials, request: Request, response: Response, db: Database) -> Account:
    try:
        user, raw = auth.login(
            db,
            body.username,
            body.password,
            request.app.state.settings,
            request.cookies.get(auth.COOKIE_NAME),
        )
    except auth.InvalidCredentialsError:
        raise HTTPException(401, "账号或密码不正确") from None
    set_session_cookie(response, raw, request)
    return account(user, request)


@router.get("/me", response_model=Account)
def me(
    request: Request, response: Response, user: Annotated[User, Depends(require_user)]
) -> Account:
    response.headers["Cache-Control"] = "no-store"
    return account(user, request)


@router.post("/logout", status_code=204, dependencies=[Depends(require_csrf)])
def logout(request: Request, response: Response, db: Database) -> None:
    auth.logout(db, request.cookies.get(auth.COOKIE_NAME))
    response.delete_cookie(
        auth.COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=request.app.state.settings.session_cookie_secure,
    )
    response.headers["Cache-Control"] = "no-store"
