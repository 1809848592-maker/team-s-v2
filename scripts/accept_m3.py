"""M3 API验收：默认全替身；显式live才读取指定Key文件并访问真实厂商。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import secrets
import signal
import socket
import sys
import tempfile
import threading
import time
from collections import defaultdict
from contextlib import ExitStack
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path
from uuid import UUID, uuid4
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
CSRF = {"X-CSRF-Protection": "1"}
REVIEWERS = (
    "spec_compliance",
    "citation_extract",
    "citation_alignment",
    "logic",
    "linkage",
)
TERM = "协作负荷"
BAD_TERM = "本报告将协作负荷定义为团队成员年龄的总和。"
LOAD_DEFINITION = "审核前一个自然日内团队需要处理的任务交接数量"
GOOD_TERM = "协作负荷是" + LOAD_DEFINITION + "。"
QUALITY_DEFINITION = "交接材料在接收时的完整性及接收方的补充沟通负担，不代表全部科研质量"
QUALITY_MEASUREMENT = (
    "交接质量仅指交接材料完整性及补充沟通负担，不代表全部科研质量。"
    "主要结果是资料完整率：接收方首次收到交付时，已实填必填字段数除以冻结的必填字段总数。"
    "同一比较统一使用结论摘要、数据及来源、给下游的说明、已知问题四项字段清单，"
    "空白或占位不计实填，比较期间不增删清单。辅助结果为首次接收时四项字段的缺漏数，"
    "以及接收后24小时内因资料缺漏发生的补充追问轮次；同一轮集中提问计一次。"
)
LOAD_ROLE = (
    "协作负荷作为审核前预定义的背景变量，可能影响交接材料的准备条件；"
    "后续比较将预先记录它，并按预注册方案分层或调整，不把审核后的负荷作为控制变量。"
    "审核条件与结果评价使用相同字段清单、评价时点及追问观察窗，避免口径随组别改变。"
)
MECHANISM = (
    "候选机制是：交付前增加一次结构化审核可能发现缺漏，提交人据此补齐材料，"
    "从而可能改善上述交接质量指标。这是待验证的机制推测，不是已经观察到的效果。"
)
VALIDATION_PLAN = (
    "本模块只交付操作定义、待检验假设及验证设计要点，尚未收集样本或任何实证数据。"
    "后续研究应在收集数据前预注册样本范围、增加一次审核与维持原流程的比较安排、"
    "协作负荷的分层或调整方案、固定指标口径及分析方法，再判断假设是否得到支持。"
    "本稿不提供效应估计、因果结论或对所有团队的推广结论。"
)
BAD_LOGIC = (
    "虽然尚未收集任何样本、对照或实证数据，我们已经证实："
    "在交付前增加一次审核必然提高所有团队的交接质量。"
)
GOOD_LOGIC = (
    "研究假设：在固定字段清单与评价时点、考虑审核前协作负荷的条件下，"
    "交付前增加一次审核可能提高资料完整率、减少字段缺漏及补充追问。"
    "这是待检验且可证伪的交接质量假设，当前没有实证数据，不构成因果结论。"
)
LOGIC_PROBLEM = "当前论断缺少样本、对照或实证证据，尚不能支持所述普遍因果结论。"
REJECTION_REASON = (
    "本模块职责是给出交接质量的操作定义、待检验假设和验证设计要点，实证检验由后续研究完成；"
    "修订稿只保留待检验假设，旧的普遍因果断言仅作为待证伪原文展示，不作为当前结论。"
)
GOOD_HANDOFF = (
    "结论：仅提出交接质量的待检验假设，附固定字段、时点、追问窗口及审核前协作负荷口径；"
    "数据来源：尚无样本或实证数据；下游：预注册后执行验证研究。"
    "已知未解决问题：假设及机制均未得到实证检验。"
)
BAD_HANDOFF = (
    "结论：" + BAD_LOGIC + "数据来源：尚无样本或实证数据，正文另列Smith与Novoid的参考条目；"
    "下游：核验这一普遍改善断言。已知未解决问题：未做实证检验。"
)
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PACKAGE_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


class AcceptanceFailure(RuntimeError):
    """Only fixed labels are exposed; never stringify provider or DB exceptions."""


class ReviewDeadline(BaseException):
    """A hard deadline must escape the component's normal exception-to-failure guard."""


def require(condition, code):
    if not condition:
        raise AcceptanceFailure(code)


def run_token_totals(rows):
    """Include every UTC day and retain unresolved reservations for this isolated run."""
    charged = sum(row.charged_tokens for row in rows)
    reserved = sum(row.reserved_tokens for row in rows if row.status in {"reserved", "unknown"})
    return charged, reserved


def model_reservation_tokens(messages, max_tokens):
    """Use the same conservative input encoding and output allowance as LLMClient."""
    encoded = json.dumps(messages, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return len(encoded) + 1024 + max_tokens


def require_run_token_budget(rows, *, token_budget, next_reserved_tokens):
    charged, reserved = run_token_totals(rows)
    require(
        charged + reserved + next_reserved_tokens <= token_budget,
        "total_run_token_budget",
    )


def citation_alignment_preseed_hit(result):
    """预埋论据缺口在第3段；context按14.9可选，不能多加R编号条件。"""
    own = result.get("alignment")
    if not isinstance(own, dict) or own.get("status") != "completed":
        return False
    findings = own.get("findings", [])
    for link in result.get("issue_links", []):
        index = link.get("finding_index")
        if type(index) is not int or not 0 <= index < len(findings):
            continue
        finding = findings[index]
        if (
            link.get("kind") == "support_insufficient"
            and finding.get("severity") == "important"
            and finding.get("location", {}).get("paragraph") == 3
            and finding.get("basis", {}).get("excerpt_verified") is True
        ):
            return True
    return False


def recovery_completed(report):
    """14.10.6恢复只验证完成，不将returned误判为链路失败。"""
    return report.get("coverage") == "completed"


def failed_e2e_for_recovery(root):
    """只补唯一真实E2E在修订稿退回后尚未执行的恢复阶段。"""
    marker_path = root / ".verification/M3-17-live-e2e-started.json"
    require(marker_path.is_file() and marker_path.stat().st_size <= 8192, "e2e_marker_required")
    marker = json.loads(marker_path.read_text())
    require(
        marker.get("revision") == "M3-17" and marker.get("layer") == "e2e",
        "e2e_marker_invalid",
    )
    directory = Path(marker["evidence"]).resolve()
    require(directory.is_relative_to((root / ".verification").resolve()), "e2e_source_invalid")
    path = directory / "report.json"
    require(path.is_file() and path.stat().st_size <= 1048576, "e2e_report_required")
    raw = path.read_bytes()
    previous = json.loads(raw)
    require(
        previous.get("revision") == "M3-17"
        and previous.get("mode") == "live"
        and previous.get("layer") == "e2e"
        and previous.get("success") is False
        and previous.get("failure", {}).get("code") == "revised_delivery_did_not_reach_final_review"
        and previous.get("real_model_network_calls", 0) > 0
        and all(
            previous.get("cleanup", {}).get(key) is True
            for key in ("schema_removed", "http_stopped", "storage_removed")
        )
        and not any(
            row.get("name") == "local_transport_disconnect_never_approves"
            for row in previous.get("steps", [])
        )
        and not (directory / "disconnected-review.json").exists()
        and not (directory / "recovered-review.json").exists(),
        "only_unexecuted_recovery_may_continue",
    )
    return {"failed_e2e_report": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def revision_file(path):
    """手动验收修订合成稿；绝不执行输入，只读取有界JSON正文。"""
    require(path.is_absolute() and path.is_file(), "revision_input_unavailable")
    require(path.stat().st_size <= 131072, "revision_input_too_large")
    data = json.loads(path.read_text(encoding="utf-8"))
    require(
        isinstance(data, dict) and set(data) == {"paragraphs", "handoff_note", "change_note"},
        "revision_input_invalid",
    )
    paragraphs = data["paragraphs"]
    require(
        isinstance(paragraphs, list)
        and 1 <= len(paragraphs) <= 100
        and all(
            isinstance(item, str) and 0 < len(item) <= 10000 and "\0" not in item
            for item in paragraphs
        ),
        "revision_input_invalid",
    )
    require(
        all(
            isinstance(data[k], str) and 0 < len(data[k]) <= 10000 and "\0" not in data[k]
            for k in ("handoff_note", "change_note")
        ),
        "revision_input_invalid",
    )
    return data


def write_json(path, value, *, sensitive=None):
    encoded = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if sensitive:
        encoded = encoded.replace(sensitive, "[REDACTED]")
    path.write_text(encoded + "\n")


def with_model_timeout(configuration, timeout_seconds):
    """Override HTTP timeouts only; preserve each validated model and pricing profile."""
    if timeout_seconds is None:
        return configuration
    values = configuration.model_dump(mode="json")
    for name in REVIEWERS:
        values["reviewers"][name]["timeout_seconds"] = timeout_seconds
    return type(configuration).model_validate(values)


def document_bytes(paragraphs, *, correct_font):
    """Small standard OPC fixture, with explicit effective run fonts and sizes."""
    props = (
        '<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" '
        'w:eastAsia="宋体" w:cs="Times New Roman"/>'
        '<w:sz w:val="24"/><w:szCs w:val="24"/>'
    )
    wrong_props = props.replace("Times New Roman", "Arial").replace("宋体", "黑体")
    content = "".join(
        f"<w:p><w:r><w:rPr>{props if correct_font or index > 0 else wrong_props}"
        '</w:rPr><w:t xml:space="preserve">'
        f"{escape(text)}</w:t></w:r></w:p>"
        for index, text in enumerate(paragraphs)
    )
    files = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.styles+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            f'<Relationships xmlns="{PACKAGE_NS}"><Relationship Id="main" '
            f'Type="{OFFICE_NS}/officeDocument" Target="word/document.xml"/></Relationships>'
        ),
        "word/_rels/document.xml.rels": (
            f'<Relationships xmlns="{PACKAGE_NS}"><Relationship Id="styles" '
            f'Type="{OFFICE_NS}/styles" Target="styles.xml"/></Relationships>'
        ),
        "word/document.xml": (
            f'<w:document xmlns:w="{W_NS}"><w:body>{content}'
            '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>'
        ),
        "word/styles.xml": (
            f'<w:styles xmlns:w="{W_NS}"><w:docDefaults><w:rPrDefault><w:rPr>{props}'
            '</w:rPr></w:rPrDefault></w:docDefaults><w:style w:type="paragraph" '
            'w:styleId="Normal" w:default="1"><w:name w:val="Normal"/></w:style></w:styles>'
        ),
    }
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text.encode())
    return output.getvalue()


def read_explicit_key(path):
    require(path is not None and path.is_absolute(), "live_requires_absolute_key_file")
    require(path.resolve() == ROOT / ".env", "key_file_must_be_platform_dotenv")
    require(
        path.is_file() and path.stat().st_size <= 65536,
        "key_file_unavailable_or_too_large",
    )
    # Read one literal assignment, never source a shell or expand/interpolate other values.
    matches = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*(?:export\s+)?DEEPSEEK_API_KEY\s*=\s*(.*?)\s*$", line)
        if match:
            value = match.group(1)
            if value[:1] in {"'", '"'}:
                require(len(value) >= 2 and value[-1] == value[0], "key_assignment_invalid")
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].strip()
            matches.append(value)
    require(len(matches) == 1, "key_assignment_missing_or_duplicate")
    value = matches[0]
    require(bool(re.fullmatch(r"[A-Za-z0-9._-]{8,512}", value)), "key_assignment_invalid")
    return value


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--mode", choices=("mock", "live"), default="mock")
    result.add_argument(
        "--database-url", required=True, help="Explicit isolated PostgreSQL test URL"
    )
    result.add_argument("--key-file", type=Path, help="Live only: absolute platform/.env path")
    result.add_argument("--output-dir", type=Path, default=ROOT / ".verification/M3-acceptance")
    result.add_argument("--max-model-calls", type=int, default=60, choices=range(1, 61))
    result.add_argument("--token-budget", type=int, default=2000000)
    result.add_argument("--model-timeout-seconds", type=int)
    result.add_argument("--layer", choices=("reviewers", "e2e", "recovery"), default="e2e")
    result.add_argument("--milestone", choices=("M3-16", "M3-17"), default="M3-16")
    result.add_argument(
        "--revision-input",
        type=Path,
        help="M3-17 manual acceptance: wait for bounded synthetic revision JSON after first report",
    )
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    require(Path.cwd() == BACKEND, "run_from_platform_backend")
    require(
        10000 <= arguments.token_budget <= 2000000,
        "token_budget_must_be_10000_to_2000000",
    )
    require(
        arguments.model_timeout_seconds is None or 1 <= arguments.model_timeout_seconds <= 180,
        "model_timeout_must_be_1_to_180_seconds",
    )
    require(
        arguments.mode == "live" or arguments.key_file is None,
        "mock_never_reads_key_files",
    )
    require(
        arguments.milestone != "M3-17" or arguments.layer in {"e2e", "recovery"},
        "m317_only_runs_resubmission_or_recovery",
    )
    require(
        arguments.layer != "recovery" or arguments.milestone == "M3-17",
        "recovery_continuation_requires_m317",
    )
    require(
        arguments.revision_input is None
        or (arguments.milestone == "M3-17" and arguments.layer == "e2e"),
        "manual_revision_requires_m317",
    )
    sys.path.insert(0, str(BACKEND))
    # Neutralize ambient settings before importing app.db/main. Never implicitly open any .env.
    os.environ["DATABASE_URL"] = arguments.database_url
    os.environ["DEEPSEEK_API_KEY"] = ""
    os.environ["LLM_ENABLE_LIVE_CALLS"] = "false"
    from app.config import Settings, get_settings

    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    import httpx2
    import uvicorn
    from pydantic import SecretStr
    from sqlalchemy import create_engine, select, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.schema import CreateSchema, DropSchema

    from app.db import get_session
    from app.main import create_app
    from app.models import Base, DailyBudget, DeliveryWorkflow, ModelCall, ReviewJob
    from app.review.citation_databases import CitationDatabaseClient, CitationQuery
    from app.review.debug_records import LocalReviewDebugStore
    from app.review.llm import LLMClient
    from app.review.model_config import load_model_config
    from app.review.runner import DeliveryReviewInput
    from app.services.llm_usage import (
        LLMCallLimitError,
        QuotaExceededError,
        QuotaLimits,
    )
    from app.services.review_jobs import claim_next, complete_job
    from app.services.review_pipeline import execute_pipeline
    from app.storage import LocalStorage

    url = make_url(arguments.database_url)
    require(url.get_backend_name() == "postgresql", "postgresql_required")
    require(
        bool(re.search(r"(?:test|smoke|accept)", url.database or "", re.IGNORECASE)),
        "explicit_test_database_required",
    )
    require(url.host in {"localhost", "127.0.0.1", "::1"}, "local_test_database_required")
    require(not url.query, "database_url_query_options_not_supported")
    configuration = with_model_timeout(load_model_config(), arguments.model_timeout_seconds)
    for name in REVIEWERS:
        require(
            configuration.reviewers[name].endpoint == "https://api.deepseek.com/chat/completions",
            "model_endpoint_not_allowed",
        )
    from accept_m3_budget import BudgetExceeded, RevisionBudget

    money = (
        RevisionBudget(ROOT / ".verification/M3-16-live-budget.json")
        if arguments.mode == "live"
        else None
    )
    if money is not None and arguments.milestone == "M3-17":
        require(
            (ROOT / ".verification/M3-16-live-budget.json").is_file(),
            "m317_requires_existing_m316_budget",
        )
        require(money.summary()["call_count"] >= 28, "m317_budget_history_missing")
    continuation = (
        failed_e2e_for_recovery(ROOT)
        if arguments.mode == "live" and arguments.layer == "recovery"
        else None
    )
    key = (
        read_explicit_key(arguments.key_file)
        if arguments.mode == "live"
        else "m3-fixed-mock-only-key"
    )
    suffix = uuid4().hex
    output = arguments.output_dir.resolve() / f"run-{datetime.now(UTC):%Y%m%d-%H%M%S}-{suffix[:8]}"
    output.mkdir(parents=True, mode=0o700)
    output.chmod(0o700)
    report = {
        "mode": arguments.mode,
        "revision": arguments.milestone,
        "layer": arguments.layer,
        "schema": f"accept_m3_{suffix}",
        "started_utc": datetime.now(UTC).isoformat(),
        "steps": [],
        "success": False,
        "continuation": continuation,
        "limits": {
            "max_model_calls": arguments.max_model_calls,
            "platform_token_budget": arguments.token_budget,
            "review_timeout_seconds": 900,
            "model_timeout_override_seconds": arguments.model_timeout_seconds,
            "model_timeout_seconds_by_reviewer": {
                name: configuration.reviewers[name].timeout_seconds for name in REVIEWERS
            },
            "automatic_job_attempts": 1,
        },
        "cleanup": {
            "schema_removed": False,
            "storage_removed": False,
            "http_stopped": False,
        },
    }
    print(f"M3 {arguments.mode}: evidence {output}", flush=True)
    logging.getLogger("httpx2").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    schema = report["schema"]
    created = False
    server = None
    server_thread = None
    listener = None
    storage_dir = None
    sessions = None
    debug_store = None
    stack = ExitStack()
    model_events, bibliography_events = [], []
    status = {"disconnected": False, "reviewer": None, "stopped": None}
    fixture_title = "ZqxvM316Acceptance20261112"
    reference = (
        f"[1] Smith, Z., & Novoid, Q. (2025). {fixture_title}. "
        "Journal of Imaginary Evidence, 99, 1-9."
    )

    def evidence(path, value):
        write_json(path, value, sensitive=key if arguments.mode == "live" else None)

    def step(name, **details):
        report["steps"].append({"name": name, "at_utc": datetime.now(UTC).isoformat(), **details})
        evidence(output / "report.json", report)
        print(f"PASS {name}", flush=True)

    def api(client, method, path, expected=200, **kwargs):
        response = client.request(method, path, **kwargs)
        require(
            response.status_code == expected,
            f"http_{method.lower()}_{response.status_code}_expected_{expected}",
        )
        return response.json()

    def typed_data(payload, kind):
        for message in payload["messages"]:
            if message["role"] == "user" and "\n" in message["content"]:
                value = json.loads(message["content"].split("\n", 1)[1])
                if value.get("kind") == kind:
                    return value["data"]
        raise AcceptanceFailure("missing_model_typed_data")

    def opinion(check_key, segment, problem, suggestion, *, severity="important", **context):
        return {
            "check_key": check_key,
            "severity": severity,
            "segment_id": segment["segment_id"],
            "problem": problem,
            "excerpt": segment["text"],
            "suggestion": suggestion,
            **context,
        }

    def mock_output(payload, name):
        delivery = typed_data(payload, "delivery")
        segments = delivery["segments"]
        revised = any(GOOD_LOGIC in item["text"] for item in segments)
        result = {"findings": [], "coverage_notes": ["外部全文及原始实证数据未核验。"]}
        if name == "spec_compliance" and not revised:
            segment = next(item for item in segments if BAD_LOGIC in item["text"])
            result["findings"] = [
                opinion("W1", segment, "把待检验假设写成已证实结论。", "明确为待检验假设。")
            ]
        elif name == "citation_extract" and not revised:
            segment = next(item for item in segments if fixture_title in item["text"])
            result["findings"] = [
                opinion(
                    "reference",
                    segment,
                    "参考文献条目。",
                    "交由数据库核验。",
                    severity="suggestion",
                )
            ]
        elif name == "citation_alignment" and not revised:
            segment = next(item for item in segments if "Smith 与 Novoid" in item["text"])
            result["findings"] = [
                opinion(
                    "support_insufficient",
                    segment,
                    "当前引用未提供可支持普遍因果判断的证据。",
                    "补充可核对证据或收窄结论。",
                    context_key="R1",
                    context_excerpt=reference,
                )
            ]
        elif name == "logic" and any(BAD_LOGIC in item["text"] for item in segments):
            segment = next(
                item for item in segments if (GOOD_LOGIC if revised else BAD_LOGIC) in item["text"]
            )
            result["findings"] = [
                opinion("support", segment, LOGIC_PROBLEM, "明确假设与证据边界。")
            ]
        elif name == "linkage" and not revised:
            segment = next(item for item in segments if BAD_TERM in item["text"])
            source = next(
                item["text"]
                for item in typed_data(payload, "bound_plan")["context_sources"]
                if item["key"] == "term:0"
            )
            result["findings"] = [
                opinion(
                    "term:0",
                    segment,
                    "协作负荷的定义与方案术语表冲突。",
                    "按方案恢复操作定义。",
                    context_key="term:0",
                    context_excerpt=source,
                )
            ]
        if arguments.milestone == "M3-17" and name == "logic" and revised:
            segment = next(item for item in segments if QUALITY_MEASUREMENT in item["text"])
            result["findings"].append(
                opinion(
                    "support",
                    segment,
                    "补充追问轮次的观察细节尚待进一步约定。",
                    "在预注册时进一步明确观察细节。",
                )
            )
        return result

    def mock_model(request):
        require(request.url.host == "api.deepseek.com", "mock_model_host_mismatch")
        name = status["reviewer"]
        model_events.append(
            {
                "reviewer": name,
                "disconnected": status["disconnected"],
                "real_network": False,
            }
        )
        if status["disconnected"]:
            raise httpx2.ConnectError("M3 local intentional disconnect", request=request)
        result = mock_output(json.loads(request.content), name)
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(result, ensure_ascii=False),
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 80,
                    "total_tokens": 200,
                    "prompt_cache_hit_tokens": 40,
                    "prompt_cache_miss_tokens": 80,
                },
            },
        )

    def mock_bibliography(request):
        require(
            request.url.host in {"api.crossref.org", "api.openalex.org"},
            "mock_bibliography_host_mismatch",
        )
        require(
            "authorization" not in request.headers,
            "bibliography_must_not_receive_model_key",
        )
        bibliography_events.append({"provider": request.url.host, "real_network": False})
        body = (
            {
                "status": "ok",
                "message-type": "work-list",
                "message": {"items": [], "total-results": 0},
            }
            if request.url.host == "api.crossref.org"
            else {"results": [], "meta": {"count": 0}}
        )
        return httpx2.Response(200, json=body)

    class RestrictedLiveTransport(httpx2.BaseTransport):
        def __init__(self):
            self.backend = None

        def handle_request(self, request):
            require(
                str(request.url) == "https://api.deepseek.com/chat/completions"
                and request.method == "POST",
                "live_model_endpoint_not_allowed",
            )
            model_events.append(
                {
                    "reviewer": status["reviewer"],
                    "disconnected": status["disconnected"],
                    "real_network": not status["disconnected"],
                }
            )
            if status["disconnected"]:
                raise httpx2.ConnectError("M3 local intentional disconnect", request=request)
            self.backend = httpx2.HTTPTransport(retries=0, trust_env=False)
            return self.backend.handle_request(request)

        def close(self):
            if self.backend is not None:
                self.backend.close()
                self.backend = None

    class RestrictedBibliographyTransport(httpx2.BaseTransport):
        def __init__(self):
            self.backend = None

        def handle_request(self, request):
            require(
                request.url.scheme == "https"
                and request.url.host in {"api.crossref.org", "api.openalex.org"}
                and request.url.port in {None, 443}
                and request.method == "GET"
                and request.url.path.startswith("/works")
                and "authorization" not in request.headers,
                "live_bibliography_endpoint_or_auth_not_allowed",
            )
            bibliography_events.append({"provider": request.url.host, "real_network": True})
            self.backend = httpx2.HTTPTransport(retries=0, trust_env=False)
            return self.backend.handle_request(request)

        def close(self):
            if self.backend is not None:
                self.backend.close()
                self.backend = None

    unit_target = None
    unit_counts = defaultdict(int)
    live_layer_claimed = False

    class BoundedClient(LLMClient):
        def complete_json(self, **kwargs):
            nonlocal live_layer_claimed
            if status["stopped"] is not None:
                raise LLMCallLimitError("验收已停止，不再外发模型请求")
            if unit_target is not None and kwargs["reviewer"] != unit_target:
                # 单人真实测试仅此审核员外发；前后置阶段用固定替身，且不伪造用量流水。
                return mock_output({"messages": kwargs["messages"]}, kwargs["reviewer"])
            if unit_target is not None and unit_counts[unit_target] >= 3:
                raise LLMCallLimitError("此审核员真实测试已达到3次上限")
            with sessions() as db:
                rows = list(db.scalars(select(ModelCall)))
            if len(rows) >= arguments.max_model_calls:
                status["stopped"] = "total_model_call_limit"
                raise LLMCallLimitError("验收总模型调用次数已达上限")
            profile = self.configuration.reviewers[kwargs["reviewer"]]
            try:
                require_run_token_budget(
                    rows,
                    token_budget=arguments.token_budget,
                    next_reserved_tokens=model_reservation_tokens(
                        kwargs["messages"], profile.max_tokens
                    ),
                )
            except AcceptanceFailure:
                status["stopped"] = "total_run_token_budget"
                raise
            status["reviewer"] = kwargs["reviewer"]
            identity = kwargs["call_id"]
            if money is not None and not status["disconnected"]:
                if not live_layer_claimed:
                    marker = (
                        ROOT
                        / f".verification/{arguments.milestone}-live-{arguments.layer}-started.json"
                    )
                    try:
                        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    except FileExistsError:
                        status["stopped"] = "live_layer_already_started_no_automatic_repeat"
                        raise AcceptanceFailure(status["stopped"]) from None
                    with os.fdopen(descriptor, "w") as stream:
                        json.dump(
                            {
                                "revision": arguments.milestone,
                                "layer": arguments.layer,
                                "evidence": str(output),
                                "started_utc": datetime.now(UTC).isoformat(),
                            },
                            stream,
                        )
                    live_layer_claimed = True
                try:
                    money.reserve(identity, kwargs["reviewer"], kwargs["messages"], profile)
                except BudgetExceeded:
                    status["stopped"] = "revision_10_cny_budget"
                    raise
            if unit_target is not None:
                unit_counts[unit_target] += 1
            try:
                return super().complete_json(**kwargs)
            except QuotaExceededError:
                status["stopped"] = "total_token_or_review_quota"
                raise
            finally:
                if money is not None and not status["disconnected"]:
                    with sessions() as db:
                        row = db.get(ModelCall, identity)
                        cost = row.estimated_cost_cny if row is not None else None
                    money.settle(identity, cost)
                    evidence(output / "revision-budget.json", money.summary())

    def ledger():
        if sessions is None:
            return None
        with sessions() as db:
            rows = list(db.scalars(select(ModelCall).order_by(ModelCall.created_at, ModelCall.id)))
            groups = defaultdict(
                lambda: {
                    "calls": 0,
                    "known_tokens": 0,
                    "unknown_usage_calls": 0,
                    "known_estimated_cost_cny": Decimal(0),
                    "unknown_cost_calls": 0,
                }
            )
            details = []
            for row in rows:
                item = groups[row.reviewer]
                item["calls"] += 1
                item["known_tokens"] += row.total_tokens or 0
                item["unknown_usage_calls"] += row.total_tokens is None
                item["known_estimated_cost_cny"] += row.estimated_cost_cny or Decimal(0)
                item["unknown_cost_calls"] += row.estimated_cost_cny is None
                details.append(
                    {
                        "call_id": str(row.id),
                        "reviewer": row.reviewer,
                        "model": row.model,
                        "status": row.status,
                        "prompt_tokens": row.prompt_tokens,
                        "completion_tokens": row.completion_tokens,
                        "total_tokens": row.total_tokens,
                        "cache_hit_tokens": row.cache_hit_tokens,
                        "cache_miss_tokens": row.cache_miss_tokens,
                        "estimated_cost_cny": str(row.estimated_cost_cny)
                        if row.estimated_cost_cny is not None
                        else None,
                        "reserved_tokens": row.reserved_tokens,
                        "charged_tokens": row.charged_tokens,
                        "error_code": row.error_code,
                    }
                )
            for item in groups.values():
                item["known_estimated_cost_cny"] = f"{item['known_estimated_cost_cny']:.10f}"
            budgets = list(
                db.scalars(select(DailyBudget).where(DailyBudget.scope_key == "platform"))
            )
            known_charge, outstanding = run_token_totals(rows)
            budget_charged = sum(row.charged_tokens for row in budgets)
            budget_reserved = sum(row.reserved_tokens for row in budgets)
            require(
                known_charge == budget_charged and outstanding == budget_reserved,
                "ledger_and_platform_budget_mismatch",
            )
            for row in rows:
                if row.estimated_cost_cny is not None:
                    require(
                        row.cache_hit_tokens is not None and row.cache_miss_tokens is not None,
                        "known_cost_missing_cache_split",
                    )
                    calculated = (
                        row.cache_hit_tokens * Decimal(row.prices["input_cache_hit"])
                        + row.cache_miss_tokens * Decimal(row.prices["input_cache_miss"])
                        + row.completion_tokens * Decimal(row.prices["output"])
                    ) / Decimal(1_000_000)
                    require(calculated == row.estimated_cost_cny, "model_call_cost_mismatch")
            return {
                "calls": len(rows),
                "known_tokens": sum(row.total_tokens or 0 for row in rows),
                "unknown_usage_calls": sum(row.total_tokens is None for row in rows),
                "known_estimated_cost_cny": format(
                    sum(
                        (row.estimated_cost_cny or Decimal(0) for row in rows),
                        Decimal(0),
                    ),
                    ".10f",
                ),
                "unknown_cost_calls": sum(row.estimated_cost_cny is None for row in rows),
                "charged_known_tokens": known_charge,
                "outstanding_reserved_tokens": outstanding,
                "conservative_budget_used_tokens": known_charge + outstanding,
                "run_token_budget": arguments.token_budget,
                "run_token_budget_respected": known_charge + outstanding <= arguments.token_budget,
                "platform_budget_reconciled": True,
                "historical_price_cost_reconciled": True,
                "by_reviewer": dict(groups),
                "calls_detail": details,
            }

    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        created = True
        scoped = engine.execution_options(schema_translate_map={None: schema})
        sessions = sessionmaker(bind=scoped, expire_on_commit=False)
        Base.metadata.create_all(scoped)
        storage_dir = Path(
            stack.enter_context(tempfile.TemporaryDirectory(prefix="teams-m3-accept-"))
        )
        storage = LocalStorage(storage_dir)
        settings = Settings(
            _env_file=None,
            database_url=arguments.database_url,
            storage_dir=storage_dir,
            static_dir=output / "no-static",
            review_debug_dir=ROOT / ".verification/M3-16-review-debug"
            if arguments.mode == "live"
            else None,
            llm_enable_live_calls=False,
            deepseek_api_key=SecretStr(""),
            review_job_timeout_seconds=900,
            review_job_max_attempts=1,
        )
        app = create_app(settings)

        def scoped_session():
            with sessions() as db:
                yield db

        app.dependency_overrides[get_session] = scoped_session
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical", access_log=False)
        )
        server_thread = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        server_thread.start()
        base = f"http://127.0.0.1:{port}"
        leader = stack.enter_context(
            httpx2.Client(base_url=base, headers=CSRF, timeout=15, trust_env=False)
        )
        member = stack.enter_context(
            httpx2.Client(base_url=base, headers=CSRF, timeout=15, trust_env=False)
        )
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        require(server.started, "local_http_server_start_failed")
        api(leader, "GET", "/api/health")
        accounts = []
        password = secrets.token_urlsafe(24)
        for client, role in ((leader, "leader"), (member, "member")):
            accounts.append(
                api(
                    client,
                    "POST",
                    "/api/auth/register",
                    201,
                    json={"username": f"m3_{role}_{suffix[:12]}", "password": password},
                )
            )
        settings.platform_admin_user_ids = accounts[0]["id"]
        notice = api(leader, "GET", "/api/model-disclosure")
        project = api(
            leader,
            "POST",
            "/api/projects",
            201,
            json={
                "name": "M3隔离验收 " + suffix[:8],
                "data_transfer_consent": True,
                "disclosure_version": notice["version"],
            },
        )
        prefix = f"/api/projects/{project['id']}"
        invitation = api(leader, "POST", prefix + "/invitations", 201, json={"role": "member"})
        api(
            member,
            "POST",
            "/api/invitations/accept",
            json={"token": invitation["token"]},
        )
        module_id = str(uuid4())
        recovery_module_id = str(uuid4())
        plan = {
            "research_question": "新增一次审核是否及如何影响科研交接质量？",
            "final_deliverable": "研究假设报告",
            "argument": (
                "围绕同一交接质量结果提出可证伪假设：新增一次审核可能改善资料完整率、"
                "字段缺漏和补充追问；先固定指标口径，将审核前协作负荷作为背景变量，"
                "后续实证检验，不能把设计或假设当作已证实的因果结论。"
            ),
            "terms": [
                {
                    "term": TERM,
                    "definition": LOAD_DEFINITION,
                    "source": "项目操作定义",
                },
                {
                    "term": "交接质量",
                    "definition": QUALITY_DEFINITION,
                    "source": "项目操作定义，以冻结字段、评价时点和追问窗口衡量",
                },
            ],
            "data_definitions": [
                {
                    "variable": "资料完整率",
                    "source": "后续研究接收方首次收到的交付记录，本稿无观测数据",
                    "processing": (
                        "首次接收时实填必填字段数/冻结必填字段总数；清单为结论摘要、数据及来源、"
                        "给下游的说明、已知问题四项，空白或占位不计实填，比较期间清单不变。"
                    ),
                },
                {
                    "variable": "字段缺漏数",
                    "source": "同一首次接收交付及冻结四项字段清单，本稿无观测数据",
                    "processing": "首次接收时四项字段中空白或仅占位的字段数；越少表示材料更完整。",
                },
                {
                    "variable": "补充追问轮次",
                    "source": "后续接收方的缺漏追问记录，本稿无观测数据",
                    "processing": (
                        "首次接收后24小时内因资料缺漏发生的追问轮次；同一轮集中提问计一次。"
                    ),
                },
                {
                    "variable": TERM,
                    "source": "审核前一个自然日的团队交接任务记录，本稿无观测数据",
                    "processing": LOAD_DEFINITION + "，仅作预定义的分层或调整背景变量。",
                },
            ],
            "modules": [
                {
                    "id": module_id,
                    "name": "假设与概念",
                    "assignee_ids": [accounts[1]["id"]],
                    "goal": (
                        "提出新增一次审核如何影响交接质量的待检验假设，"
                        "给出资料完整率、字段缺漏、补充追问的操作定义，"
                        "说明审核前协作负荷的背景变量角色及后续验证要点；本模块不要求提供实证结果。"
                    ),
                    "scope_in": "概念操作定义、待检验假设、候选机制和预注册验证设计要点",
                    "scope_out": "实证数据收集与因果估计",
                    "core_argument": (
                        "新增一次审核可能通过发现并补齐缺漏改善交接质量，仅为待检验假设；"
                        "在固定字段、时点及窗口下，以资料完整率为主结果，"
                        "字段缺漏和补充追问为辅助结果，将审核前协作负荷作为背景变量，后续验证。"
                    ),
                    "upstream": [],
                    "delivery_format": "docx",
                }
            ],
        }
        plan["modules"].append(
            {
                **plan["modules"][0],
                "id": recovery_module_id,
                "name": "断网恢复验证",
            }
        )
        api(
            leader,
            "POST",
            prefix + "/plan/versions",
            201,
            json={"change_note": "验收方案", "content": plan},
        )
        specifications = [
            {
                "number": "F1",
                "category": "format",
                "requirement": "文稿中文正文使用宋体、英文正文使用 Times New Roman，均为12磅。",
                "check_method": "program",
            },
            {
                "number": "W1",
                "category": "writing",
                "requirement": "研究假设须明确标为待检验假设，不能写成已经证实的结论。",
                "check_method": "ai",
            },
        ]
        api(
            leader,
            "POST",
            prefix + "/spec/versions",
            201,
            json={"change_note": "验收规范", "items": specifications},
        )
        step(
            "isolated_http_project_plan_spec_and_current_consent",
            project_id=project["id"],
            module_id=module_id,
            recovery_module_id=recovery_module_id,
            disclosure_version=notice["version"],
        )

        transport = (
            httpx2.MockTransport(mock_model)
            if arguments.mode == "mock"
            else RestrictedLiveTransport()
        )
        debug_store = LocalReviewDebugStore(
            ROOT / ".verification/M3-16-review-debug"
            if arguments.mode == "live"
            else storage_dir / "review-debug"
        )
        debug_store.purge_expired()
        model = BoundedClient(
            sessions,
            configuration=configuration,
            secrets={"DEEPSEEK_API_KEY": SecretStr(key)},
            live_enabled=arguments.mode == "live",
            transport=transport,
            debug_store=debug_store,
            quota_limits=QuotaLimits(
                project_daily_reviews=3,
                platform_daily_reviews=3,
                platform_daily_tokens=arguments.token_budget,
                max_calls_per_review=arguments.max_model_calls,
            ),
        )
        database = CitationDatabaseClient(
            transport=httpx2.MockTransport(mock_bibliography)
            if arguments.mode == "mock"
            else RestrictedBibliographyTransport(),
            timeout_seconds=8,
        )
        if arguments.layer != "recovery":
            fixture_verification = database.verify(
                CitationQuery(fixture_title, ("Smith, Z.", "Novoid, Q."), 2025, None, False)
            )
            evidence(output / "fixture-verification.json", fixture_verification.to_dict())
            require(
                fixture_verification.status == "not_found"
                and {source.provider for source in fixture_verification.sources}
                == {"crossref", "openalex"}
                and all(source.status == "no_match" for source in fixture_verification.sources),
                "fabricated_fixture_database_preflight_failed",
            )
        bad = document_bytes(
            [
                "科研交接研究的概念与假设。",
                BAD_TERM,
                BAD_LOGIC,
                QUALITY_MEASUREMENT,
                LOAD_ROLE,
                MECHANISM,
                "本稿尚未收集样本或实证数据，也未实施对照，仍将第3段视为已证实且适用于所有团队的结论。",
                "前述提升的判断引用了 Smith 与 Novoid (2025) [1]。",
                "References",
                reference,
            ],
            correct_font=False,
        )
        revised = document_bytes(
            [
                "科研交接研究的概念与假设。",
                GOOD_TERM,
                GOOD_LOGIC
                + " 原待检验命题原文：“"
                + BAD_LOGIC
                + "”。该绝对表述仅作为待证伪对象，不是已经成立的结论。",
                QUALITY_MEASUREMENT,
                LOAD_ROLE,
                MECHANISM,
                VALIDATION_PLAN,
            ],
            correct_font=True,
        )
        recovery = document_bytes(
            [
                "科研交接研究的概念与假设。",
                GOOD_TERM,
                GOOD_LOGIC,
                QUALITY_MEASUREMENT,
                LOAD_ROLE,
                MECHANISM,
                VALIDATION_PLAN,
            ],
            correct_font=True,
        )
        (output / "problematic.docx").write_bytes(bad)
        (output / "revised.docx").write_bytes(revised)
        (output / "connection-recovery.docx").write_bytes(recovery)

        def submit(data, name, *, handoff_note=GOOD_HANDOFF, target_module_id=module_id):
            item = api(
                member,
                "POST",
                prefix + f"/modules/{target_module_id}/deliveries",
                201,
                files={
                    "file": (
                        name,
                        data,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
                data={
                    "ai_participation": "none",
                    "ai_note": "验收合成材料，由验收驱动生成并核对。",
                    "handoff_note": handoff_note,
                    "disclosure_version": notice["version"],
                },
            )
            require(
                item["sha256"] == hashlib.sha256(data).hexdigest(),
                "submitted_bytes_hash_mismatch",
            )
            return item

        def current(item):
            return next(
                row for row in api(member, "GET", prefix + "/deliveries") if row["id"] == item["id"]
            )

        def execute(item, label):
            started = time.monotonic()
            with sessions() as db:
                claimed = claim_next(db, timeout_seconds=900, max_attempts=1, retry_delay_seconds=0)
                db.commit()
            require(
                claimed is not None and str(claimed.delivery_id) == item["id"],
                "queue_claim_mismatch",
            )
            review = DeliveryReviewInput(
                claimed.job_id,
                claimed.delivery_id,
                claimed.project_id,
                claimed.attempt,
                claimed.workflow_revision,
            )

            def timeout_handler(signum, frame):
                raise ReviewDeadline

            previous_handler = signal.signal(signal.SIGALRM, timeout_handler)
            signal.setitimer(signal.ITIMER_REAL, 900)
            try:
                pointer = execute_pipeline(
                    sessions,
                    review,
                    storage=storage,
                    client=model,
                    database_client=database,
                )
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_handler)
            require(time.monotonic() - started < 900, "review_900_second_limit")
            with sessions() as db:
                require(
                    complete_job(db, claimed, pointer, retry_delay_seconds=0),
                    "queue_completion_rejected",
                )
                db.commit()
            result = api(member, "GET", prefix + f"/deliveries/{item['id']}/review")
            evidence(output / f"{label}-review.json", result)
            measured = ledger()
            evidence(output / "usage.json", measured)
            require(measured["run_token_budget_respected"], "run_token_budget_exceeded")
            require(status["stopped"] is None, status["stopped"] or "budget_stopped")
            return result, current(item)

        if arguments.layer == "reviewers":
            from app.services.citation_review import review_delivery_citations
            from app.services.reasoning_review import review_delivery_reasoning
            from app.services.spec_review import review_delivery_spec

            first = submit(bad, "reviewer-problematic.docx", handoff_note=BAD_HANDOFF)
            with sessions() as db:
                job = db.scalar(select(ReviewJob).where(ReviewJob.delivery_id == UUID(first["id"])))
                job_id = job.id
            common = dict(
                project_id=UUID(project["id"]),
                delivery_id=UUID(first["id"]),
                job_id=job_id,
                storage=storage,
                client=model,
                history=[],
            )
            unit_results = {}
            for reviewer in REVIEWERS:
                unit_target = reviewer
                trials = []
                successes = 0
                while unit_counts[reviewer] < 3 and successes < 2:
                    before = unit_counts[reviewer]
                    if reviewer == "spec_compliance":
                        result = review_delivery_spec(sessions, **common)
                        own = result["review"]
                        hit = bool(
                            own
                            and any(
                                row["severity"] in {"important", "blocking"}
                                and row["basis"].get("spec_number") == "W1"
                                and row["location"].get("paragraph") == 3
                                and row["basis"].get("excerpt_verified") is True
                                for row in own["findings"]
                            )
                        )
                    elif reviewer in {"citation_extract", "citation_alignment"}:
                        result = review_delivery_citations(
                            sessions, **common, database_client=database
                        )
                        own = result[
                            "extraction" if reviewer == "citation_extract" else "alignment"
                        ]
                        if reviewer == "citation_extract":
                            hit = any(
                                row["title"] == fixture_title
                                and row["verification"]["status"] == "not_found"
                                for row in result["references"]
                            )
                        else:
                            hit = citation_alignment_preseed_hit(result)
                    else:
                        result = review_delivery_reasoning(sessions, **common)
                        own = result[reviewer]
                        hit = bool(
                            own
                            and any(
                                link["check_key"]
                                == ("support" if reviewer == "logic" else "term:0")
                                and own["findings"][link["finding_index"]]["severity"]
                                == "important"
                                and own["findings"][link["finding_index"]]["location"].get(
                                    "paragraph"
                                )
                                == (3 if reviewer == "logic" else 2)
                                and own["findings"][link["finding_index"]]["basis"].get(
                                    "excerpt_verified"
                                )
                                is True
                                for link in result["finding_links"][reviewer]
                            )
                        )
                    require(unit_counts[reviewer] > before, "unit_reviewer_did_not_call")
                    valid = bool(
                        own
                        and own["status"] == "completed"
                        and not any(
                            "条意见因格式无效未采用" in note
                            for note in own["coverage"]["uncovered"]
                        )
                    )
                    successes += valid and hit
                    trials.append(
                        {
                            "trial": len(trials) + 1,
                            "model_calls": unit_counts[reviewer] - before,
                            "structurally_valid": valid,
                            "preseed_hit": bool(hit),
                            "success": bool(valid and hit),
                        }
                    )
                    evidence(output / f"unit-{reviewer}-{len(trials)}.json", result)
                    require(status["stopped"] is None, status["stopped"] or "budget_stopped")
                unit_results[reviewer] = {
                    "passed": successes >= 2,
                    "successes": successes,
                    "model_calls": unit_counts[reviewer],
                    "trials": trials,
                }
                evidence(output / "reviewer-results.json", unit_results)
                step("reviewer_" + reviewer + "_measured", **unit_results[reviewer])
            unit_target = None
            report["reviewer_results"] = unit_results
        elif arguments.layer == "e2e":
            first = submit(bad, "problematic.docx", handoff_note=BAD_HANDOFF)
            first_report, first_state = execute(first, "first")
            require(
                first_report.get("coverage") == "completed"
                and first_state["review_status"] == "returned",
                "first_review_not_completed_and_returned",
            )
            findings = first_report["opinions"]
            seen = {
                "font": any(
                    row["opinion"].get("reviewer") == "format_checker"
                    and row["opinion"]["severity"] == "blocking"
                    for row in findings
                ),
                "fabricated_reference": any(
                    row["opinion"].get("reviewer") == "citation_database"
                    and row["opinion"]["severity"] == "blocking"
                    for row in findings
                ),
                "term_conflict": any(
                    row["opinion"]["category"] == "linkage"
                    and TERM
                    in (row["opinion"]["problem"] + (row["opinion"]["basis"].get("excerpt") or ""))
                    for row in findings
                ),
                "unsupported_argument": any(
                    row["opinion"]["category"] == "logic" for row in findings
                ),
            }
            require(
                all(seen.values()),
                "first_review_did_not_identify_all_four_expected_problems",
            )
            step(
                "first_real_pipeline_identifies_four_problems_and_returns",
                checks=seen,
                opinions=len(findings),
            )
            rejected_ids = []
            for finding in findings:
                rejected = finding["opinion"]["category"] == "logic"
                annotation = api(
                    member,
                    "POST",
                    prefix
                    + f"/deliveries/{first['id']}/review/findings/"
                    + f"{finding['finding_id']}/annotation",
                    json={
                        "handling": "not_adopted" if rejected else "adopted",
                        "reason": REJECTION_REASON if rejected else "已在修订版修改。",
                        "expected_revision": finding["annotation_revision"],
                    },
                )
                require(
                    annotation["handling"] == ("not_adopted" if rejected else "adopted"),
                    "annotation_not_saved",
                )
                if rejected:
                    rejected_ids.append(finding["finding_id"])
            require(
                rejected_ids and len(findings) - len(rejected_ids) >= 2,
                "insufficient_adopted_and_rejected_annotations",
            )
            step(
                "human_adopts_repairs_and_rejects_logic_with_reason",
                not_adopted_ids=rejected_ids,
            )
            revision_handoff = GOOD_HANDOFF
            if arguments.revision_input is not None:
                print(f"READY revision input: {arguments.revision_input}", flush=True)
                wait_until = time.monotonic() + 900
                while not arguments.revision_input.exists() and time.monotonic() < wait_until:
                    time.sleep(0.25)
                manual = revision_file(arguments.revision_input)
                revised = document_bytes(manual["paragraphs"], correct_font=True)
                revision_handoff = manual["handoff_note"]
                (output / "revised.docx").write_bytes(revised)
                evidence(
                    output / "revision-change-note.json",
                    {
                        "change_note": manual["change_note"],
                        "input_sha256": hashlib.sha256(
                            arguments.revision_input.read_bytes()
                        ).hexdigest(),
                    },
                )
            second = submit(revised, "revised.docx", handoff_note=revision_handoff)
            second_report, second_state = execute(second, "revised")
            require(
                second_report.get("coverage") == "completed"
                and second_state["review_status"] == "pending_final_review",
                "revised_delivery_did_not_reach_final_review",
            )
            require(
                not second_report.get("nonconverged"),
                "cannot_accept_loop_bypass_as_clean_review",
            )
            final = api(leader, "GET", prefix + f"/deliveries/{second['id']}/review/final")
            evidence(output / "revised-final-context.json", final)
            if arguments.milestone == "M3-17":
                downgraded = [
                    row
                    for row in second_report["opinions"]
                    if row["opinion"].get("convergence_note")
                ]
                require(
                    all(
                        row["opinion"]["severity"] == "suggestion"
                        and row["opinion"].get("original_severity") in {"important", "blocking"}
                        for row in downgraded
                    ),
                    "convergence_did_not_preserve_original_severity",
                )
                if arguments.mode == "mock":
                    require(downgraded, "mock_resubmission_did_not_exercise_convergence")
                report["convergence"] = {
                    "downgraded_opinions": len(downgraded),
                    "resubmission_reached_final_review": True,
                }

            retained = [row for row in final["not_adopted"] if row["reason"] == REJECTION_REASON]
            require(
                retained and final["can_decide"],
                "rejected_history_missing_from_final_review",
            )
            step(
                "revision_preserves_rejection_without_blocking_final_review",
                duplicate_links=sum(
                    row["duplicate_of"] is not None for row in second_report["opinions"]
                ),
                retained_rejections=len(retained),
            )
            decision = api(
                leader,
                "POST",
                prefix + f"/deliveries/{second['id']}/review/final",
                json={
                    "decision": "approve",
                    "comment": "已核对合成验收交付及全部不采纳理由，采用本版本。",
                    "expected_revision": final["workflow_revision"],
                    "context_hash": final["context_hash"],
                    "spec_rulings": [
                        {
                            "finding_id": row["finding_id"],
                            "decision": "accept",
                            "comment": "负责人核对后接受例外理由。",
                        }
                        for row in final["specification_exceptions"]
                    ],
                },
            )
            evidence(output / "final-decision.json", decision)
            require(
                current(second)["review_status"] == "adopted",
                "final_human_approval_not_adopted",
            )
            step("leader_final_review_adopts_revised_delivery")

        if arguments.layer in {"e2e", "recovery"}:
            interrupted = submit(
                recovery,
                "connection-recovery.docx",
                target_module_id=recovery_module_id,
            )
            status["disconnected"] = True
            failed_report, failed_state = execute(interrupted, "disconnected")
            require(
                failed_report.get("coverage") == "incomplete"
                and failed_state["review_status"] == "ai_review_partial_failure",
                "disconnected_model_false_approval",
            )
            require(
                failed_state["review_progress"]["status"] == "failed",
                "disconnected_job_not_failed",
            )
            step("local_transport_disconnect_never_approves", model_network_requests=0)
            status["disconnected"] = False
            with sessions() as db:
                revision = db.get(DeliveryWorkflow, UUID(interrupted["id"])).revision
            queued = api(
                member,
                "POST",
                prefix + f"/deliveries/{interrupted['id']}/review/retry",
                json={"expected_revision": revision},
            )
            require(queued["status"] == "retry_wait", "public_retry_did_not_queue")
            recovered_report, recovered_state = execute(interrupted, "recovered")
            require(
                recovery_completed(recovered_report),
                "recovery_did_not_complete",
            )
            step(
                "public_human_retry_restores_completed_review",
                preserved_attempt=queued["attempt"],
                recovered_attempt=recovered_state["review_progress"]["attempt"],
                recovered_delivery_state=recovered_state["review_status"],
                criterion="coverage=completed (design 14.10.6)",
            )
        measured = ledger()
        with sessions() as db:
            days = list(db.scalars(select(ModelCall.day)))
        query = {
            "start": str(min(days)),
            "end": str(max(days)),
            "project_id": project["id"],
        }
        exported = api(leader, "GET", "/api/admin/usage", params=query)
        require(
            exported["totals"]["calls"] == measured["calls"]
            and exported["totals"]["total_tokens"] == measured["known_tokens"]
            and exported["totals"]["unknown_usage_calls"] == measured["unknown_usage_calls"]
            and exported["totals"]["unpriced_calls"] == measured["unknown_cost_calls"]
            and exported["totals"]["estimated_cost_cny"] == measured["known_estimated_cost_cny"],
            "admin_usage_does_not_reconcile",
        )
        for group in exported["by_reviewer"]:
            expected = measured["by_reviewer"][group["reviewer"]]
            require(
                group["calls"] == expected["calls"]
                and group["total_tokens"] == expected["known_tokens"]
                and group["estimated_cost_cny"] == expected["known_estimated_cost_cny"],
                "admin_reviewer_usage_does_not_reconcile",
            )
        response = leader.get("/api/admin/usage.csv", params=query)
        require(response.status_code == 200, "admin_usage_csv_not_available")
        exported_rows = list(csv.DictReader(StringIO(response.content.decode("utf-8-sig"))))
        require(
            {row["call_id"] for row in exported_rows}
            == {row["call_id"] for row in measured["calls_detail"]}
            and len(exported_rows) == measured["calls"]
            and sum(int(row["total_tokens"]) for row in exported_rows if row["total_tokens"])
            == measured["known_tokens"]
            and sum(
                (
                    Decimal(row["estimated_cost_cny"])
                    for row in exported_rows
                    if row["estimated_cost_cny"]
                ),
                Decimal(0),
            )
            == Decimal(measured["known_estimated_cost_cny"]),
            "admin_usage_csv_does_not_reconcile",
        )
        evidence(output / "admin-usage.json", exported)
        (output / "usage.csv").write_bytes(response.content)
        step(
            "administrator_summary_csv_and_reviewer_costs_reconcile",
            rows=len(exported_rows),
        )
        report["success"] = arguments.layer in {"e2e", "recovery"} or all(
            row["passed"] for row in unit_results.values()
        )
    except (Exception, ReviewDeadline) as error:
        report["failure"] = {
            "type": type(error).__name__,
            "code": str(error)
            if isinstance(error, AcceptanceFailure)
            else status["stopped"]
            or (
                "review_deadline_900_seconds"
                if isinstance(error, ReviewDeadline)
                else "operation_failed_details_withheld"
            ),
        }
        print(f"FAIL {report['failure']['type']}: {report['failure']['code']}", flush=True)
    finally:
        try:
            if debug_store is not None:
                records = debug_store.list()
                report["debug_records"] = len(records)
            if money is not None:
                report["revision_budget"] = money.summary()
            report["usage"] = ledger()
            evidence(output / "usage.json", report["usage"])
            if report["usage"] is not None and not report["usage"]["run_token_budget_respected"]:
                report["success"] = False
                report.setdefault(
                    "failure",
                    {"type": "AcceptanceFailure", "code": "run_token_budget_exceeded"},
                )
            report["model_transport"] = model_events
            report["real_model_network_calls"] = sum(row["real_network"] for row in model_events)
            report["bibliography_transport"] = bibliography_events
            report["real_bibliography_network_requests"] = sum(
                row["real_network"] for row in bibliography_events
            )
        except Exception as error:
            report["evidence_error_type"] = type(error).__name__
            report["success"] = False
        if server is not None:
            server.should_exit = True
        if server_thread is not None:
            server_thread.join(timeout=10)
            report["cleanup"]["http_stopped"] = not server_thread.is_alive()
        if listener is not None:
            listener.close()
        try:
            stack.close()
        except Exception as error:
            report["cleanup"]["storage_or_http_close_error_type"] = type(error).__name__
        report["cleanup"]["storage_removed"] = storage_dir is None or not storage_dir.exists()
        if created:
            try:
                with engine.begin() as connection:
                    connection.execute(DropSchema(schema, cascade=True))
                with engine.connect() as connection:
                    report["cleanup"]["schema_removed"] = not connection.scalar(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM information_schema.schemata "
                            "WHERE schema_name=:name)"
                        ),
                        {"name": schema},
                    )
            except Exception as error:
                report["cleanup"]["error_type"] = type(error).__name__
        engine.dispose()
        report["finished_utc"] = datetime.now(UTC).isoformat()
        report["success"] = report["success"] and all(
            report["cleanup"].get(name)
            for name in ("http_stopped", "schema_removed", "storage_removed")
        )
        evidence(output / "report.json", report)
        key = ""
    print(
        json.dumps(
            {
                "success": report["success"],
                "mode": arguments.mode,
                "steps": len(report["steps"]),
                "usage": {
                    key: value
                    for key, value in (report.get("usage") or {}).items()
                    if key not in {"calls_detail", "by_reviewer"}
                },
                "cleanup": report["cleanup"],
                "evidence": str(output),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if report["success"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AcceptanceFailure as error:
        print(f"FAIL {error}", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as error:
        print(f"FAIL {type(error).__name__}: details withheld", file=sys.stderr)
        raise SystemExit(2) from None
