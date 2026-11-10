from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.events import record_event
from app.models import AuthSession, User
from app.security import new_token, password_hash, token_hash, verify_password

COOKIE_NAME = "teams_v2_session"
# 未注册账号也执行一次相同的密码运算，避免以快速失败泄露账号存在性。
_DUMMY_HASH = password_hash("unused-password-for-timing")


class UsernameTakenError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


class UnauthenticatedError(Exception):
    pass


def _create_session(db: Session, user: User, settings: Settings, previous_token: str | None) -> str:
    now = datetime.now(UTC)
    db.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
    if previous_token:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash(previous_token)))
    raw = new_token()
    db.add(
        AuthSession(
            token_hash=token_hash(raw),
            user_id=user.id,
            expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
        )
    )
    return raw


def register(
    db: Session,
    username: str,
    password: str,
    settings: Settings,
    previous_token: str | None = None,
) -> tuple[User, str]:
    user = User(
        username=username, username_key=username.casefold(), password_hash=password_hash(password)
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise UsernameTakenError from error
    raw = _create_session(db, user, settings, previous_token)
    record_event(
        db,
        actor_id=user.id,
        event_type="user_registered",
        object_type="user",
        object_id=user.id,
        details={"username": user.username},
    )
    db.commit()
    return user, raw


def login(
    db: Session,
    username: str,
    password: str,
    settings: Settings,
    previous_token: str | None = None,
) -> tuple[User, str]:
    user = db.scalar(select(User).where(User.username_key == username.casefold()))
    valid = verify_password(password, user.password_hash if user else _DUMMY_HASH)
    if not user or not valid:
        raise InvalidCredentialsError
    raw = _create_session(db, user, settings, previous_token)
    record_event(
        db, actor_id=user.id, event_type="user_logged_in", object_type="user", object_id=user.id
    )
    db.commit()
    return user, raw


def current_user(db: Session, raw: str | None) -> User:
    if not raw:
        raise UnauthenticatedError
    user = db.scalar(
        select(User)
        .join(AuthSession, AuthSession.user_id == User.id)
        .where(
            AuthSession.token_hash == token_hash(raw),
            AuthSession.expires_at > datetime.now(UTC),
        )
    )
    if not user:
        raise UnauthenticatedError
    return user


def logout(db: Session, raw: str | None) -> None:
    if raw:
        user_id = db.scalar(
            delete(AuthSession)
            .where(AuthSession.token_hash == token_hash(raw))
            .returning(AuthSession.user_id)
        )
        if user_id is not None:
            record_event(
                db,
                actor_id=user_id,
                event_type="user_logged_out",
                object_type="user",
                object_id=user_id,
            )
            db.commit()
