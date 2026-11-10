import os
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.config import Settings
from app.db import get_session
from app.main import create_app
from app.models import Base
from app.models.auth import AuthSession, User
from app.security import token_hash, verify_password
from app.services.auth import COOKIE_NAME

CSRF_HEADERS = {"X-CSRF-Protection": "1"}
PASSWORD = "test-only-password"


@dataclass
class AuthEnvironment:
    client: TestClient
    sessions: sessionmaker[Session]
    make_client: Callable[..., TestClient]


@pytest.fixture
def auth_environment(tmp_path) -> Iterator[AuthEnvironment]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TEST_DATABASE_URL to an isolated PostgreSQL test database")
    if make_url(database_url).get_backend_name() != "postgresql":
        pytest.fail("Authentication tests require PostgreSQL, not SQLite")

    # Only the explicitly supplied test database is touched. Each test owns its schema.
    engine = create_engine(database_url, pool_pre_ping=True)
    schema = f"test_auth_{uuid4().hex}"
    with engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    scoped_engine = engine.execution_options(schema_translate_map={None: schema})
    sessions = sessionmaker(bind=scoped_engine, expire_on_commit=False)
    try:
        Base.metadata.create_all(scoped_engine)
        with ExitStack() as stack:

            def make_client(**overrides) -> TestClient:
                values = {
                    "database_url": database_url,
                    "static_dir": tmp_path / "no-static-frontend",
                    "session_ttl_seconds": 3600,
                    "session_cookie_secure": False,
                    "platform_admin_user_ids": "",
                }
                values.update(overrides)
                app = create_app(Settings(_env_file=None, **values))

                def override_session():
                    with sessions() as session:
                        yield session

                app.dependency_overrides[get_session] = override_session
                scheme = "https" if values["session_cookie_secure"] else "http"
                return stack.enter_context(TestClient(app, base_url=f"{scheme}://testserver"))

            yield AuthEnvironment(make_client(), sessions, make_client)
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        engine.dispose()


def register(client: TestClient, username: str = "researcher"):
    return client.post(
        "/api/auth/register",
        json={"username": username, "password": PASSWORD},
        headers=CSRF_HEADERS,
    )


def login(client: TestClient, username: str = "researcher", password: str = PASSWORD):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        headers=CSRF_HEADERS,
    )


def test_register_stores_only_password_and_session_hashes(auth_environment):
    client = auth_environment.client
    response = register(client)

    assert response.status_code == 201
    identity = response.json()
    assert set(identity) == {"id", "username", "is_admin"}
    assert identity["username"] == "researcher"
    assert identity["is_admin"] is False
    assert client.get("/api/auth/me").json() == identity

    raw_token = client.cookies.get(COOKIE_NAME)
    assert raw_token
    with auth_environment.sessions() as session:
        user = session.get(User, UUID(identity["id"]))
        assert user.password_hash != PASSWORD
        assert verify_password(PASSWORD, user.password_hash)
        stored_session = session.get(AuthSession, token_hash(raw_token))
        assert stored_session is not None
        assert stored_session.user_id == user.id
        assert stored_session.token_hash != raw_token
        assert stored_session.expires_at > datetime.now(UTC)

    cookies = SimpleCookie()
    cookies.load(response.headers["set-cookie"])
    cookie = cookies[COOKIE_NAME]
    assert cookie["httponly"]
    assert cookie["path"] == "/"
    assert cookie["samesite"].lower() == "lax"
    assert int(cookie["max-age"]) == 3600
    assert not cookie["secure"]


def test_missing_or_forged_session_is_rejected(auth_environment):
    client = auth_environment.client
    assert client.get("/api/auth/me").status_code == 401
    assert (
        client.get("/api/auth/me", headers={"Cookie": f"{COOKIE_NAME}=forged-token"}).status_code
        == 401
    )


def test_wrong_password_and_unknown_account_have_same_response(auth_environment):
    client = auth_environment.client
    assert register(client).status_code == 201
    assert client.post("/api/auth/logout", headers=CSRF_HEADERS).status_code == 204

    wrong_password = login(client, password="wrong-password")
    unknown_account = login(client, username="missing-user")
    assert wrong_password.status_code == unknown_account.status_code == 401
    assert wrong_password.json() == unknown_account.json()
    assert COOKIE_NAME not in client.cookies
    assert client.get("/api/auth/me").status_code == 401


def test_expired_server_session_is_rejected(auth_environment):
    client = auth_environment.client
    assert register(client).status_code == 201
    raw_token = client.cookies.get(COOKIE_NAME)
    with auth_environment.sessions() as session:
        stored_session = session.get(AuthSession, token_hash(raw_token))
        stored_session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    assert client.get("/api/auth/me").status_code == 401


def test_logout_revokes_session_and_is_idempotent(auth_environment):
    client = auth_environment.client
    assert register(client).status_code == 201
    raw_token = client.cookies.get(COOKIE_NAME)
    response = client.post("/api/auth/logout", headers=CSRF_HEADERS)

    assert response.status_code == 204
    assert COOKIE_NAME not in client.cookies
    assert client.get("/api/auth/me").status_code == 401
    assert (
        client.get("/api/auth/me", headers={"Cookie": f"{COOKIE_NAME}={raw_token}"}).status_code
        == 401
    )
    with auth_environment.sessions() as session:
        assert session.get(AuthSession, token_hash(raw_token)) is None
    assert client.post("/api/auth/logout", headers=CSRF_HEADERS).status_code == 204


def test_login_rotates_the_previous_session(auth_environment):
    client = auth_environment.client
    identity = register(client).json()
    old_token = client.cookies.get(COOKIE_NAME)
    response = login(client)

    assert response.status_code == 200
    assert response.json() == identity
    assert client.cookies.get(COOKIE_NAME) != old_token
    assert client.get("/api/auth/me").status_code == 200
    assert (
        client.get("/api/auth/me", headers={"Cookie": f"{COOKIE_NAME}={old_token}"}).status_code
        == 401
    )


def test_username_normalization_prevents_duplicate_accounts(auth_environment):
    client = auth_environment.client
    first = register(client, username="  Researcher  ")
    assert first.status_code == 201
    assert register(client, username="rEsEaRcHeR").status_code == 409

    with auth_environment.sessions() as session:
        assert len(session.scalars(select(User)).all()) == 1
    assert client.post("/api/auth/logout", headers=CSRF_HEADERS).status_code == 204
    response = login(client, username=" researcher ")
    assert response.status_code == 200
    assert response.json()["id"] == first.json()["id"]


def test_admin_privilege_comes_only_from_server_configuration(auth_environment):
    client = auth_environment.client
    forbidden = client.post(
        "/api/auth/register",
        json={"username": "researcher", "password": PASSWORD, "is_admin": True},
        headers=CSRF_HEADERS,
    )
    assert forbidden.status_code == 422
    identity = register(client).json()
    assert identity["is_admin"] is False

    admin_client = auth_environment.make_client(
        platform_admin_user_ids=f" {identity['id']} , {uuid4()} "
    )
    response = login(admin_client)
    assert response.status_code == 200
    assert response.json()["is_admin"] is True
    assert admin_client.get("/api/auth/me").json()["is_admin"] is True

    no_admin_client = auth_environment.make_client(platform_admin_user_ids="")
    response = login(no_admin_client)
    assert response.status_code == 200
    assert response.json()["is_admin"] is False


def test_secure_session_cookie_is_configurable(auth_environment):
    client = auth_environment.make_client(session_cookie_secure=True)
    response = register(client)
    assert response.status_code == 201
    cookies = SimpleCookie()
    cookies.load(response.headers["set-cookie"])
    assert cookies[COOKIE_NAME]["secure"]
    assert cookies[COOKIE_NAME]["httponly"]
    assert client.get("/api/auth/me").status_code == 200


def test_authentication_writes_require_csrf_header_and_same_origin(auth_environment):
    client = auth_environment.client
    credentials = {"username": "researcher", "password": PASSWORD}
    for endpoint in ("register", "login", "logout"):
        path = f"/api/auth/{endpoint}"
        assert client.post(path, json=credentials).status_code == 403
        assert (
            client.post(path, json=credentials, headers={"X-CSRF-Protection": "0"}).status_code
            == 403
        )
        assert (
            client.post(
                path,
                json=credentials,
                headers={**CSRF_HEADERS, "Origin": "https://attacker.invalid"},
            ).status_code
            == 403
        )
    response = client.post(
        "/api/auth/register",
        json=credentials,
        headers={**CSRF_HEADERS, "Origin": "http://testserver"},
    )
    assert response.status_code == 201


def test_invalid_credentials_are_rejected_without_creating_accounts(auth_environment):
    client = auth_environment.client
    for credentials in (
        {"username": "researcher", "password": "short"},
        {"username": "researcher", "password": "p" * 257},
        {"username": "   ", "password": PASSWORD},
    ):
        response = client.post("/api/auth/register", json=credentials, headers=CSRF_HEADERS)
        assert response.status_code == 422
        assert f'"{credentials["password"]}"' not in response.text
        assert all("input" not in item and "ctx" not in item for item in response.json()["detail"])
    with auth_environment.sessions() as session:
        assert session.scalars(select(User)).all() == []
