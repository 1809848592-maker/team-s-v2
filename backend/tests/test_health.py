from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.db import get_session
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<h1>Team.s</h1>", encoding="utf-8")
    app = create_app(Settings(static_dir=static_dir))
    session = Mock()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client, session


def test_health_reports_database_ready(client):
    test_client, session = client
    response = test_client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert str(session.execute.call_args.args[0]) == "SELECT 1"


def test_database_failure_is_not_reported_as_healthy(client):
    test_client, session = client
    session.execute.side_effect = OperationalError("SELECT 1", {}, Exception("db unavailable"))
    response = test_client.get("/api/health")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_static_frontend_does_not_shadow_api(client):
    test_client, _ = client
    assert "Team.s" in test_client.get("/").text
    assert test_client.get("/api/unknown").status_code == 404
    assert "/api/health" in test_client.get("/openapi.json").json()["paths"]
