import os
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.config import Settings
from app.db import get_session
from app.main import create_app
from app.models import Base


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
    schema = f"test_m1_{uuid4().hex}"
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
