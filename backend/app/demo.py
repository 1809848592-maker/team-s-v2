"""CLI-only fictional dataset. Fixed review responses never enter the live worker."""

import hashlib
import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.config import Settings
from app.events import record_event
from app.models import Base, DeliveryVersion
from app.presets.catalog import preset_catalog
from app.review.runner import DeliveryReviewInput
from app.services import (
    agent_actions,
    agents,
    auth,
    collaboration,
    deliveries,
    final_review,
    format_check,
    plans,
    projects,
    review_jobs,
    review_reports,
    specs,
)
from app.services.model_disclosure import DISCLOSURE_VERSION

DEMO_PASSWORD = "TeamsDemo-Only-2026"
ACCOUNTS = {"leader": "demo-leader", "writer": "demo-writer", "analyst": "demo-analyst"}
NOTICE = "虚构演示数据；审核使用固定响应，未调用真实模型，不作研究结论。"
LIMIT = 1024 * 1024


class DemoError(ValueError):
    pass


def validate_demo_url(raw):
    try:
        url = make_url(raw)
        if url.drivername != "postgresql+psycopg" or url.database != "teams_demo" or url.query:
            raise ValueError
    except (TypeError, ValueError):
        raise DemoError("必须显式使用无额外连接参数的独立PostgreSQL演示库teams_demo。") from None
    return url


@dataclass(frozen=True)
class Dataset:
    project_id: UUID
    user_ids: dict[str, UUID]
    module_ids: dict[str, UUID]
    adopted_id: UUID
    storage_identity: str | None = None

    @property
    def accounts(self):
        return dict(ACCOUNTS)

    @property
    def password(self):
        return DEMO_PASSWORD

    def to_manifest(self):
        return {
            "format": 1,
            "project_id": str(self.project_id),
            "user_ids": {key: str(value) for key, value in self.user_ids.items()},
            "module_ids": {key: str(value) for key, value in self.module_ids.items()},
            "adopted_id": str(self.adopted_id),
            "accounts": self.accounts,
            "notice": NOTICE,
            "storage_identity": self.storage_identity,
        }


def _module(identity, name, user_id, deadline, goal, upstream=()):
    return {
        "id": identity,
        "name": name,
        "assignee_ids": [user_id],
        "deadline": deadline,
        "goal": goal,
        "core_argument": "围绕交接完整度讨论协作效率；演示数据不支持实证推断。",
        "scope_in": goal,
        "scope_out": "不代写其他模块，不把虚构数据当作真实调查。",
        "upstream": list(upstream),
        "downstream_handoff": "交接结论、模拟数据口径及尚未解决的问题。",
        "delivery_format": "md",
    }


def _plan(people, modules, now):
    first = _module(
        modules["literature"],
        "文献与概念",
        people["writer"],
        now + timedelta(days=1),
        "界定协作效率、交接完整度与AI辅助。",
    )
    second = _module(
        modules["analysis"],
        "模拟数据分析",
        people["analyst"],
        now + timedelta(days=2),
        "展示模拟数据口径和描述性分析。",
        [{"module_id": first["id"], "needs": "术语定义和概念之间的关系。"}],
    )
    third = _module(
        modules["integration"],
        "统稿与课堂汇报",
        people["leader"],
        now - timedelta(days=1),
        "统合文献与模拟分析，形成课堂汇报。",
        [
            {"module_id": first["id"], "needs": "已采用的概念说明。"},
            {"module_id": second["id"], "needs": "模拟分析结论与局限。"},
        ],
    )
    return {
        "change_note": f"创建演示方案。{NOTICE}",
        "content": {
            "research_question": "人机混合科研小组如何通过明确交接减少协作返工？（虚构演示）",
            "final_deliverable": "课堂演示报告与汇报材料",
            "deadline": now + timedelta(days=7),
            "argument": f"概念界定→模拟分析→整合汇报。{NOTICE}",
            "terms": [
                {
                    "term": "协作效率",
                    "definition": "在明确任务范围下完成交接的相对效率。",
                    "source": "演示自定义",
                },
                {
                    "term": "交接完整度",
                    "definition": "结论、来源、口径和待办的完整程度。",
                    "source": "演示自定义",
                },
                {
                    "term": "AI辅助",
                    "definition": "由人承担责任，智能体提供辅助。",
                    "source": "演示自定义",
                },
            ],
            "data_definitions": [
                {
                    "variable": "模拟样本",
                    "source": "脚本虚构的30条记录",
                    "processing": "仅展示流程，不代表真实学生。",
                },
                {
                    "variable": "返工次数",
                    "source": "脚本虚构的示例值",
                    "processing": "描述性展示，不计算因果效应。",
                },
            ],
            "modules": [first, second, third],
        },
    }


def _spec():
    citation = next(item for item in preset_catalog() if item["key"] == "citation_gbt7714_2015")
    return {
        "change_note": "发布演示规范，保留范例库引用预设。",
        "items": [
            {
                "number": "W1",
                "category": "writing",
                "check_method": "ai",
                "requirement": "使用方案中的统一术语。",
            },
            {
                "number": "W2",
                "category": "writing",
                "check_method": "ai",
                "requirement": "论文体正文不保留课堂演示提示。",
            },
            {
                "number": "T1",
                "category": "template",
                "check_method": "manual",
                "requirement": "汇报从范例库选用团队模板。",
            },
            {
                "number": "F1",
                "category": "format",
                "check_method": "program",
                "requirement": "正文使用宋体、小四号；Markdown不适用字体检查。",
            },
            {
                "number": "H1",
                "category": "handoff",
                "check_method": "ai",
                "requirement": "交接说明列出结论、来源和下游待办。",
            },
            *citation["items"],
        ],
    }


def _opinion(category, problem, excerpt, *, severity="important", spec_number=None):
    return {
        "finding_id": str(uuid4()),
        "category": category,
        "severity": severity,
        "location": {"description": "演示正文", "part": "document", "paragraph": 1},
        "problem": problem,
        "basis": {"excerpt": excerpt, **({"spec_number": spec_number} if spec_number else {})},
        "suggestion": f"请核对后采纳或说明不采纳理由。{NOTICE}",
        "reviewer": "spec_compliance" if category == "specification" else "logic",
        "instruction_version": "demo-fixed-1",
        "model": "demo-fixed-response",
        "input_hash": hashlib.sha256(excerpt.encode()).hexdigest(),
        "instruction_hash": hashlib.sha256(NOTICE.encode()).hexdigest(),
        "call_id": str(uuid4()),
    }


def _review(sessions, storage, delivery, findings):
    with sessions() as db:
        claim = review_jobs.claim_next(db)
        if claim is None or claim.delivery_id != delivery.id:
            raise DemoError("演示任务领取顺序异常。")
        db.commit()
    completed = {"status": "completed", "findings": [], "coverage_notes": [NOTICE]}
    components = {
        "format": format_check.inspect_delivery_format(
            sessions, project_id=delivery.project_id, delivery_id=delivery.id, storage=storage
        ),
        "specification": {
            "status": "completed",
            "review": {
                **completed,
                "findings": [f for f in findings if f["category"] == "specification"],
            },
        },
        "citations": {
            "status": "completed",
            "extraction": completed,
            "alignment": completed,
            "database_findings": [],
            "demo_notice": NOTICE,
        },
        "reasoning": {
            "status": "completed",
            "logic": {
                **completed,
                "findings": [f for f in findings if f["category"] != "specification"],
            },
            "linkage": completed,
        },
    }
    reference = review_reports.stage_report(
        sessions,
        DeliveryReviewInput(
            claim.job_id,
            claim.delivery_id,
            claim.project_id,
            claim.attempt,
            claim.workflow_revision,
        ),
        components,
    )
    with sessions() as db:
        if not review_jobs.complete_job(db, claim, reference):
            raise DemoError("演示审核未完成。")
        db.commit()
        return review_reports.get_report_internal(db, db.get(DeliveryVersion, delivery.id))


def create_dataset(sessions, storage, *, now=None, storage_identity=None):
    """Only an empty application schema; caller owns the outer transaction and storage boundary."""
    now = now or datetime.now(UTC)
    settings = Settings(_env_file=None, deepseek_api_key="", llm_enable_live_calls=False)
    with sessions() as db:
        if any(db.execute(select(table).limit(1)).first() for table in Base.metadata.sorted_tables):
            raise DemoError("目标库已有数据，拒绝追加或覆盖演示数据。")
    people, modules = {}, {name: uuid4() for name in ("literature", "analysis", "integration")}
    for role, username in ACCOUNTS.items():
        with sessions() as db:
            user, _ = auth.register(db, username, DEMO_PASSWORD, settings)
            people[role] = user.id
    with sessions() as db:
        project = projects.create_project(
            db,
            "科研交接演示（虚构数据／固定响应审核）",
            people["leader"],
            data_transfer_consent=True,
            disclosure_version=DISCLOSURE_VERSION,
        )
    for role in ("writer", "analyst"):
        with sessions() as db:
            invitation = projects.create_invitation(db, project.id, people["leader"], "member")
            projects.accept_invitation(db, invitation, people[role])
    with sessions() as db:
        plans.publish_plan(db, project.id, people["leader"], _plan(people, modules, now))
        specs.publish_spec(db, project.id, people["leader"], _spec())
    handoff = f"结论：交接需明确术语；来源：虚构演示资料；下游：核对口径后整合。{NOTICE}"

    def submit(role, module, content):
        with sessions() as db:
            return deliveries.submit_delivery(
                db,
                project.id,
                modules[module],
                people[role],
                f"{module}-演示.md",
                io.BytesIO(content.encode()),
                {
                    "ai_participation": "assisted",
                    "ai_note": NOTICE,
                    "handoff_note": handoff,
                    "disclosure_version": DISCLOSURE_VERSION,
                },
                storage=storage,
                max_bytes=LIMIT,
            )

    old_text = f"本演示把协作效率简称为协同速度。\n\n{NOTICE}\n"
    old = submit("writer", "literature", old_text)
    problem = _opinion("logic", "协同速度未与方案中的协作效率定义对齐。", "协同速度")
    exception = _opinion(
        "specification", "W2要求论文体正文移除课堂演示提示。", "演示", spec_number="W2"
    )
    report = _review(sessions, storage, old, [problem, exception])
    for finding in report["opinions"]:
        accepted = finding["opinion"]["category"] != "specification"
        with sessions() as db:
            review_reports.annotate_finding(
                db,
                project.id,
                old.id,
                UUID(finding["finding_id"]),
                people["writer"],
                handling="adopted" if accepted else "not_adopted",
                expected_revision=0,
                reason="统一为方案中的协作效率。"
                if accepted
                else "课堂演示必须保留虚构数据和固定响应声明，本场景不适用论文体要求。",
            )
    revised = submit("writer", "literature", f"协作效率采用整体方案的定义。\n\n{NOTICE}\n")
    _review(
        sessions,
        storage,
        revised,
        [exception | {"finding_id": str(uuid4()), "call_id": str(uuid4())}],
    )
    with sessions() as db:
        context = final_review.get_final_review(db, project.id, revised.id, people["leader"])
        final_review.decide_final_review(
            db,
            project.id,
            revised.id,
            people["leader"],
            {
                "decision": "approve",
                "expected_revision": context["workflow_revision"],
                "context_hash": context["context_hash"],
                "comment": "保留演示声明，已核对术语修订；此采用仅作操作演示。",
                "spec_rulings": [
                    {
                        "finding_id": entry["finding_id"],
                        "decision": "accept",
                        "comment": "认可演示场景不适用。",
                    }
                    for entry in context["specification_exceptions"]
                ],
            },
        )
    analysis = submit(
        "analyst", "analysis", f"30条虚构记录仅用于描述演示，不推断因果。\n\n{NOTICE}\n"
    )
    _review(
        sessions,
        storage,
        analysis,
        [
            _opinion(
                "logic", "可在汇报中补充模拟分析的适用范围。", "30条虚构记录", severity="suggestion"
            )
        ],
    )
    with sessions() as db:
        agent, _ = agents.register_agent(
            db,
            project.id,
            people["analyst"],
            {"name": "演示助手（示例已吊销）", "client_type": "demo"},
        )
        agent_actions.report_progress(
            db,
            agent,
            modules["analysis"],
            {"stage": "checking", "summary": "模拟分析已提交，等待人类终审。"},
        )
        agent_actions.upload_draft(
            db,
            agent,
            modules["analysis"],
            "模拟分析补充-待本人确认.md",
            io.BytesIO(f"待本人确认的补充演示稿。{NOTICE}".encode()),
            handoff,
            storage=storage,
            max_bytes=LIMIT,
        )
        agents.revoke_agent(db, project.id, agent.id, people["analyst"])
    with sessions() as db:
        deliveries.upload_material(
            db,
            project.id,
            people["leader"],
            "演示阅读说明.md",
            io.BytesIO(
                f"{NOTICE}\n三个演示账号共享独立演示库。\n新提交不会自动获得固定响应，真实模型关闭。".encode()
            ),
            storage=storage,
            max_bytes=LIMIT,
        )
    for status, title in (
        ("resolved", "核对概念口径"),
        ("responded", "确认模拟数据的展示范围"),
        ("open", "补充统稿交接清单"),
    ):
        with sessions() as db:
            request = collaboration.create_request(
                db,
                project.id,
                people["writer"],
                {
                    "title": title,
                    "content": f"请核对后回应。{NOTICE}",
                    "target_module_id": modules["analysis"],
                    "expected_by": now - timedelta(hours=2),
                },
            )
            if status != "open":
                collaboration.accept_request(db, project.id, request.id, people["analyst"])
                collaboration.respond_request(
                    db,
                    project.id,
                    request.id,
                    people["analyst"],
                    {"content": "已核对，仅作虚构演示，不代表真实研究。"},
                )
            if status == "resolved":
                collaboration.resolve_request(db, project.id, request.id, people["writer"])
    dataset = Dataset(project.id, people, modules, revised.id, storage_identity)
    with sessions() as db:
        record_event(
            db,
            project_id=project.id,
            actor_id=people["leader"],
            event_type="demo_seeded",
            object_type="project",
            object_id=project.id,
            details=dataset.to_manifest(),
        )
        db.commit()
    return dataset
