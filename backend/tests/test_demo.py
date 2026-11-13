"""M4-05 isolated fictional data; no live providers or operator .env."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.demo import DemoError, create_dataset, validate_demo_url
from app.models import AgentDraft, Base, DeliveryVersion, DeliveryWorkflow, Event, Project, User
from app.models.review_report import ReviewAnnotation, ReviewReport
from app.services import auth, draft_confirmation, final_review, plans, review_reports, specs
from app.storage import LocalStorage


def snapshot(environment):
    with environment.sessions() as db:
        return {
            table.name: list(db.execute(select(table).order_by(*table.primary_key.columns)))
            for table in Base.metadata.sorted_tables
        }


def test_demo_is_real_versioned_workflow_and_private_agent_draft(auth_environment, tmp_path):
    environment = auth_environment
    result = create_dataset(environment.sessions, LocalStorage(tmp_path / "demo"))
    project_id, people = result.project_id, result.user_ids
    with environment.sessions() as db:
        project = db.get(Project, project_id)
        assert "演示" in project.name and "固定响应" in project.name
        plan = plans.get_plan(db, project_id, people["leader"])
        assert len(plan.content["modules"]) == 3
        assert len(plan.content["terms"]) == 3 and len(plan.content["data_definitions"]) == 2
        spec = specs.get_spec(db, project_id, people["leader"])
        assert {item["category"] for item in spec.items} == {
            "writing",
            "format",
            "template",
            "handoff",
        }
        states = list(db.scalars(select(DeliveryWorkflow.state)))
        assert sorted(states) == ["adopted", "pending_final_review", "returned"]
        assert len(list(db.scalars(select(ReviewAnnotation)))) == 2
        versions = list(
            db.scalars(
                select(DeliveryVersion).order_by(DeliveryVersion.module_id, DeliveryVersion.version)
            )
        )
        assert sorted(item.version for item in versions) == [1, 1, 2]
        for delivery in versions:
            report = review_reports.get_report_internal(db, delivery)
            for opinion in report["opinions"]:
                assert opinion["opinion"]["model"] == "demo-fixed-response"
            path = LocalStorage(tmp_path / "demo").path(delivery.sha256)
            assert path.is_file() and "演示" in path.read_text()
        draft = db.scalar(select(AgentDraft))
        assert draft.owner_id == people["analyst"]
        assert len(draft_confirmation.list_drafts(db, project_id, people["analyst"])) == 1
        assert draft_confirmation.list_drafts(db, project_id, people["leader"]) == []
        final = final_review.get_final_review(db, project_id, result.adopted_id, people["leader"])
        assert final["decision"]["outcome"] == "adopted"
        events = list(db.scalars(select(Event).order_by(Event.id)))
        kinds = {event.event_type for event in events}
        assert {
            "plan_published",
            "spec_published",
            "delivery_submitted",
            "delivery_final_reviewed",
        } <= kinds
        assert any(event.actor_type == "agent" for event in events)
        assert len(list(db.scalars(select(User)))) == 3
        assert len(list(db.scalars(select(ReviewReport)))) == 3


def test_demo_passwords_login_through_existing_authentication(auth_environment, tmp_path):
    result = create_dataset(auth_environment.sessions, LocalStorage(tmp_path / "demo"))
    with auth_environment.sessions() as db:
        for role, username in result.accounts.items():
            user, _ = auth.login(
                db, username, result.password, auth_environment.client.app.state.settings
            )
            assert user.id == result.user_ids[role]


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///teams_demo",
        "postgresql+psycopg://teams:x@localhost/teams",
        "postgresql+psycopg://teams:x@localhost/teams_dev",
        "postgresql+psycopg://teams:x@localhost/teams_m4_test",
        "postgresql+psycopg://teams:x@localhost/teams_demo?options=-csearch_path%3Dpublic",
    ],
)
def test_non_demo_database_or_connection_override_is_rejected(url):
    with pytest.raises(DemoError):
        validate_demo_url(url)


def test_demo_url_allows_only_explicit_dedicated_database():
    assert (
        validate_demo_url("postgresql+psycopg://teams:x@postgres:5432/teams_demo").database
        == "teams_demo"
    )


def test_cli_rejects_missing_explicit_target_and_never_reads_dotenv(tmp_path):
    import os
    import subprocess
    import sys

    backend = Path(__file__).resolve().parents[1]
    sentinel = "PRIVATE_ENV_SENTINEL_DO_NOT_PRINT"
    (tmp_path / ".env").write_text(
        f"DATABASE_URL=postgresql://{sentinel}@real-data/production\n"
        f"DEEPSEEK_API_KEY={sentinel}\nLLM_ENABLE_LIVE_CALLS=true\n"
    )
    environment = {
        key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "TMPDIR"}
    }
    environment["PYTHONPATH"] = str(backend)
    for extra in (
        {},
        {
            "DEMO_DATABASE_URL": "postgresql+psycopg://teams:x@127.0.0.1:1/production",
            "DEMO_STORAGE_DIR": str(tmp_path / "not-created"),
        },
    ):
        result = subprocess.run(
            [sys.executable, "-m", "app.demo_bootstrap"],
            cwd=tmp_path,
            env={**environment, **extra},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "演示初始化失败" in result.stderr
        assert sentinel not in result.stdout + result.stderr
        assert not (tmp_path / "not-created").exists()


def test_seeding_refuses_existing_non_demo_data_without_writes(auth_environment, tmp_path):
    client = auth_environment.client
    assert (
        client.post(
            "/api/auth/register",
            json={"username": "real-user", "password": "keep-this-password"},
            headers={"X-CSRF-Protection": "1"},
        ).status_code
        == 201
    )
    before = snapshot(auth_environment)
    with pytest.raises(DemoError):
        create_dataset(auth_environment.sessions, LocalStorage(tmp_path / "demo"))
    assert snapshot(auth_environment) == before


def test_demo_metadata_contains_no_session_or_agent_credentials(auth_environment, tmp_path):
    result = create_dataset(auth_environment.sessions, LocalStorage(tmp_path / "demo"))
    serialized = json.dumps(result.to_manifest())
    assert "tsv2_agent_" not in serialized and "token" not in serialized
    assert "password" not in serialized
    assert not Path(tmp_path / "demo" / ".env").exists()


def test_demo_never_constructs_live_model_or_citation_clients(
    auth_environment, tmp_path, monkeypatch
):
    from app.models import ModelCall
    from app.review import citation_databases, factory

    def forbidden(*args, **kwargs):
        pytest.fail("Demo must not construct a live provider or citation database client")

    monkeypatch.setattr(factory, "build_llm_client", forbidden)
    monkeypatch.setattr(citation_databases, "CitationDatabaseClient", forbidden)
    create_dataset(auth_environment.sessions, LocalStorage(tmp_path / "demo"))
    with auth_environment.sessions() as db:
        assert list(db.scalars(select(ModelCall))) == []
